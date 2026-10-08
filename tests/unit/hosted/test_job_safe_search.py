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

pytest.importorskip("eth_account")
pytest.importorskip("x402")

from eth_account import Account  # noqa: E402
from x402.mechanisms.evm.exact.client import ExactEvmScheme  # noqa: E402
from x402.schemas import PaymentPayload, ResourceInfo  # noqa: E402

from hosted.job_safe_search.contract import SearchRequest, atomic  # noqa: E402
from hosted.job_safe_search.economics import financial_state  # noqa: E402
from hosted.job_safe_search.metrics import report  # noqa: E402
from hosted.job_safe_search.payments import encode  # noqa: E402
from hosted.job_safe_search.service import Config, SearchService, create_app  # noqa: E402
from hosted.job_safe_search.store import Refused, Store  # noqa: E402
from hosted.job_safe_search.supplier import SupplierResult  # noqa: E402

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
        price=15000,
        pay_to=pay_to,
        resource_url="https://search.example.com/search",
        token_secret=b"s" * 32,
        realized_payment_fee=0,
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
    assert row(service)["state"] == (
        "SERVICE_FAILED" if failure in ("empty", "bad_url", "cost_overrun") else "SUPPLIER_UNKNOWN"
    )
    assert service.supplier.calls == 1
    assert chain.balance_of(service.config.pay_to) == 15000
    assert f["settled_revenue"] == "0.015" and f["realized_margin"] is None
    assert f["supplier_cogs"] == (
        "0.008"
        if failure == "cost_overrun"
        else "0.007"
        if failure in ("empty", "bad_url")
        else None
    )
    assert f["unresolved_liability"] == "0.015"


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
        replace(service.config, network="eip155:8453", realized_payment_fee=None).validate(
            service.supplier
        )
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
            realized_payment_fee=0,
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
            payload='{"accepted":{"network":"eip155:8453"}}',
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


@pytest.mark.asyncio
async def test_credit_liability_and_refund_reconciliation(system: Any) -> None:
    service, account, _ = system
    await service.handle(body(), payment(service, account))
    service.store.resolve_financials(
        1, supplier_cogs=7000, variable_fees=0, credits=1000, evidence="test credit ledger"
    )
    assert financial_state(row(service))["realized_margin"] is None
    assert result(service.response(row(service)))["receipt"]["financial_state"] == "UNRESOLVED"
    assert (
        report(service.store.path, set(), network="eip155:84532")[
            "realized_external_contribution_margin_usd"
        ]
        is None
    )
    with pytest.raises(ValueError, match="confirmed transaction"):
        service.store.resolve_financials(
            1, supplier_cogs=7000, variable_fees=0, refunds=15000, evidence="refund pending"
        )
    service.store.resolve_financials(
        1,
        supplier_cogs=7000,
        variable_fees=0,
        refunds=15000,
        refund_transaction="confirmed-test-refund",
        evidence="test refund and supplier reconciled",
    )
    assert financial_state(row(service))["realized_margin"] == "-0.007"
    assert row(service)["state"] == "DELIVERED"


@pytest.mark.asyncio
async def test_unknown_payment_cannot_be_resolved_or_claimed_collected(system: Any) -> None:
    service, account, _ = system
    service.chain.final = False
    response = result(await service.handle(body(), payment(service, account)))
    assert response["receipt"]["charged_usd"] is None
    with pytest.raises(ValueError, match="finality"):
        service.store.resolve_financials(
            1, supplier_cogs=0, variable_fees=0, evidence="not chain confirmed"
        )
    assert service.supplier.calls == 0


@pytest.mark.asyncio
async def test_changed_supplier_envelope_blocks_recovery_spend(system: Any) -> None:
    service, account, _ = system
    service.chain.final = False
    await service.handle(body(), payment(service, account))
    service.chain.final = True
    service.supplier.max_cost = 8000
    await service.recover("0x0")
    assert service.supplier.calls == 0
    assert row(service)["state"] == "FINALITY_PENDING"


@pytest.mark.asyncio
@pytest.mark.parametrize("credits", [0, 1, None])
async def test_serpex_bounded_plain_search_billing(credits: int | None) -> None:
    from hosted.job_safe_search.supplier import SerpexSearch

    supplier = SerpexSearch("fixture-key", 800)
    await supplier.client.aclose()

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"q": "machine budgets", "include_content": False}
        return httpx.Response(
            200,
            json={
                "id": "billing-reference",
                "results": [{"title": "Result", "url": "https://example.com", "snippet": "useful"}],
                "metadata": {"credits_used": credits},
            },
        )

    supplier.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    actual = await supplier.search(body())
    assert actual.cogs == (None if credits is None else credits * 800)
    assert actual.provider_request_id == "billing-reference"
    await supplier.client.aclose()


def test_exclusion_file_labels_and_fail_closed(tmp_path: Path) -> None:
    from hosted.job_safe_search.metrics import excluded_wallets

    path = tmp_path / "excluded.txt"
    with pytest.raises(ValueError):
        excluded_wallets(path)
    path.write_text("0x" + "a" * 40 + " TESTNET_CONTROLLED\n# comment\n")
    assert excluded_wallets(path) == {"0x" + "a" * 40}


@pytest.mark.asyncio
async def test_background_recovery_does_not_freeze_active_supplier(system: Any) -> None:
    service, account, _ = system
    service.supplier.failure = "crash"
    with pytest.raises(KeyboardInterrupt):
        await service.handle(body(), payment(service, account))
    await service.recover("0x0", startup=False)
    assert row(service)["state"] == "SUPPLIER_INFLIGHT"
    await service.recover("0x0")
    assert row(service)["state"] == "SUPPLIER_UNKNOWN"
    assert service.supplier.calls == 1


@pytest.mark.asyncio
async def test_proxy_pre_settled_payment_requires_chain_evidence(system: Any) -> None:
    from hosted.job_safe_search.payments import decode

    service, account, chain = system
    service.config = replace(service.config, recovery_from_block="0x0")
    signed = payment(service, account)
    await service.facilitator.inner.settle(decode(signed), service.requirements)
    first = result(await service.handle(body(), signed))
    assert first["receipt"]["economic_state"] == "SETTLED"
    assert chain.balance_of(service.config.pay_to) == 15000
    assert service.facilitator.settles == 0 and service.supplier.calls == 1
    assert financial_state(row(service))["realized_margin"] == "0.008"


@pytest.mark.asyncio
async def test_expired_authorization_and_facilitator_outage_preserve_capital(system: Any) -> None:
    service, account, chain = system
    service.facilitator.failure = "verify"
    signed = payment(service, account)
    await service.handle(body(), signed)
    assert row(service)["state"] == "RESERVED"
    assert service.facilitator.settles == service.supplier.calls == 0
    service.facilitator.failure = None
    chain.clock = lambda: 9999999999
    await service.handle(body(), signed)
    assert row(service)["state"] == "PAYMENT_REJECTED"
    assert financial_state(row(service))["realized_margin"] == "0"
    assert service.facilitator.settles == service.supplier.calls == 0


@pytest.mark.asyncio
async def test_pre_settled_proxy_waits_for_finality_without_recharging(system: Any) -> None:
    from hosted.job_safe_search.payments import decode

    service, account, chain = system
    service.config = replace(service.config, recovery_from_block="0x0")
    signed = payment(service, account)
    await service.facilitator.inner.settle(decode(signed), service.requirements)
    service.chain.final = False
    response = result(await service.handle(body(), signed))
    assert response["receipt"]["original_charge_usd"] is None
    assert row(service)["state"] == "FINALITY_PENDING"
    assert service.supplier.calls == service.facilitator.settles == 0
    service.chain.final = True
    await service.recover("0x0")
    assert row(service)["state"] == "DELIVERED"
    assert chain.balance_of(service.config.pay_to) == 15000


@pytest.mark.asyncio
async def test_proxy_pre_settled_payment_recovers_if_verify_raises(system: Any) -> None:
    from hosted.job_safe_search.payments import decode

    service, account, chain = system
    service.config = replace(service.config, recovery_from_block="0x0")
    signed = payment(service, account)
    await service.facilitator.inner.settle(decode(signed), service.requirements)
    service.facilitator.failure = "verify"
    await service.handle(body(), signed)
    assert row(service)["state"] == "DELIVERED"
    assert service.supplier.calls == 1 and service.facilitator.settles == 0
    assert chain.balance_of(service.config.pay_to) == 15000


@pytest.mark.asyncio
async def test_extra_proxy_settlement_is_not_a_silent_duplicate(system: Any) -> None:
    from hosted.job_safe_search.payments import decode

    service, account, chain = system
    service.config = replace(service.config, recovery_from_block="0x0")
    await service.handle(body(), payment(service, account))
    fresh = payment(service, account)
    await service.facilitator.inner.settle(decode(fresh), service.requirements)
    for _ in range(2):
        response = result(await service.handle(body(), fresh))
        assert response["receipt"]["additional_payment_liability_usd"] == "0.015"
        assert response["receipt"]["financial_state"] == "UNRESOLVED"
    assert chain.balance_of(service.config.pay_to) == 30000
    assert service.supplier.calls == service.facilitator.settles == 1
    metrics = report(service.store.path, set(), network="eip155:84532")
    assert metrics["gross_external_settled_revenue_usd"] == "0.03"
    assert metrics["additional_settled_payments"] == 1
    assert metrics["known_realized_external_contribution_margin_usd"] == "0"
    assert metrics["realized_external_contribution_margin_usd"] is None


@pytest.mark.asyncio
async def test_unfinalized_duplicate_payment_survives_restart_as_liability(system: Any) -> None:
    from hosted.job_safe_search.payments import decode

    service, account, _ = system
    service.config = replace(service.config, recovery_from_block="0x0")
    await service.handle(body(), payment(service, account))
    fresh = payment(service, account)
    await service.facilitator.inner.settle(decode(fresh), service.requirements)
    service.chain.final = False
    await service.handle(body(), fresh)
    assert financial_state(row(service))["realized_margin"] is None
    metrics = report(service.store.path, set(), network="eip155:84532")
    assert metrics["additional_pending_payments"] == 1
    assert metrics["gross_external_settled_revenue_usd"] == "0.015"
    assert metrics["realized_external_contribution_margin_usd"] is None
    service.chain.final = True
    await service.recover("0x0")
    metrics = report(service.store.path, set(), network="eip155:84532")
    assert metrics["additional_pending_payments"] == 0
    assert metrics["additional_settled_payments"] == 1
    assert metrics["gross_external_settled_revenue_usd"] == "0.03"
    assert service.supplier.calls == service.facilitator.settles == 1


@pytest.mark.asyncio
async def test_metrics_default_excludes_testnet_even_for_unknown_wallet(system: Any) -> None:
    service, account, _ = system
    await service.handle(body(), payment(service, account))
    metrics = report(service.store.path, set())
    assert metrics["network"] == "eip155:8453"
    assert metrics["financial_unit"] == "USDC"
    assert metrics["external_paid_calls"] == 0
    assert metrics["gross_external_settled_revenue_usd"] == "0"
    assert metrics["realized_external_contribution_margin_usd"] == "0"
    modeled = report(service.store.path, set(), network="eip155:84532")
    assert modeled["financial_unit"] == "TEST_USDC"
    assert modeled["external_paid_calls"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("refund_fee, expected_margin", [(100, "0.0079"), (9000, "-0.001")])
async def test_extra_payment_refund_needs_evidence_and_preserves_actual_margin(
    system: Any, refund_fee: int, expected_margin: str
) -> None:
    from hosted.job_safe_search.payments import decode

    service, account, _ = system
    service.config = replace(service.config, recovery_from_block="0x0")
    await service.handle(body(), payment(service, account))
    fresh = payment(service, account)
    await service.facilitator.inner.settle(decode(fresh), service.requirements)
    await service.handle(body(), fresh)
    nonce = decode(fresh).payload["authorization"]["nonce"].lower()
    with pytest.raises(ValueError):
        service.store.reconcile_extra_refund(
            1, nonce, refund_transaction="", variable_fees=0, evidence=""
        )
    service.store.reconcile_extra_refund(
        1,
        nonce,
        refund_transaction="confirmed-test-refund",
        variable_fees=refund_fee,
        evidence="test full refund evidence",
    )
    metrics = report(service.store.path, set(), network="eip155:84532")
    assert metrics["gross_external_settled_revenue_usd"] == "0.03"
    assert metrics["refunds_usd"] == "0.015"
    assert metrics["realized_external_contribution_margin_usd"] == expected_margin
    assert metrics["additional_payment_liability_usd"] == "0"
    assert metrics["negative_margin_requests"] == (1 if refund_fee == 9000 else 0)
    assert financial_state(row(service))["realized_margin"] == expected_margin
    with pytest.raises(ValueError, match="already"):
        service.store.reconcile_extra_refund(
            1, nonce, refund_transaction="another", variable_fees=refund_fee, evidence="duplicate"
        )


@pytest.mark.asyncio
async def test_recovery_rpc_failure_isolated_per_purchase(system: Any) -> None:
    service, account, chain = system
    service.facilitator.failure = "crash_after_settle"
    for request_id in ("one", "two"):
        with pytest.raises(KeyboardInterrupt):
            await service.handle(body(request_id), payment(service, account))
    first_nonce = row(service)["nonce"]
    original = service.chain.confirmed

    async def confirm(payload: Any, tx: str) -> bool:
        if payload.payload["authorization"]["nonce"].lower() == first_nonce:
            raise RuntimeError("one receipt unavailable")
        return await original(payload, tx)

    service.chain.confirmed = confirm
    await service.recover("0x0")
    await service.recover("0x0")
    assert row(service)["state"] == "SETTLING"
    assert financial_state(row(service))["realized_margin"] is None
    assert financial_state(service.store.get(2))["realized_margin"] == "0.008"
    assert service.facilitator.settles == 2 and service.supplier.calls == 1
    assert chain.balance_of(service.config.pay_to) == 30000
    with service.store.connect() as c:
        assert (
            c.execute("SELECT COUNT(*) FROM events WHERE kind='RECOVERY_DEFERRED'").fetchone()[0]
            == 1
        )


@pytest.mark.asyncio
async def test_startup_rpc_outage_keeps_completed_replay_available(system: Any) -> None:
    service, account, _ = system
    signature = payment(service, account)
    await service.handle(body(), signature)
    service.facilitator.failure = "crash_after_settle"
    with pytest.raises(KeyboardInterrupt):
        await service.handle(body("two"), payment(service, account))
    service.config = replace(service.config, recovery_from_block="0x0")
    service.chain.unavailable = True
    app = create_app(service)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        replay = await service.handle(body(), signature)
        assert replay.status_code == 200
        assert result(replay)["receipt"]["charged_usd"] == "0"
        assert financial_state(service.store.get(2))["realized_margin"] is None
        assert service.facilitator.settles == 2 and service.supplier.calls == 1


@pytest.mark.asyncio
async def test_chain_scan_is_bounded_and_continues_finalized_history(system: Any) -> None:
    from hosted.job_safe_search.payments import USED, ChainEvidence, decode

    service, account, _ = system
    payload = decode(payment(service, account))
    chain = ChainEvidence("https://rpc.example.invalid", service.requirements)
    chain.log_block_span = 3
    chain.max_scan_requests = 2
    ranges = []

    async def rpc(method: str, params: list[Any]) -> Any:
        if method == "eth_chainId":
            return hex(84532)
        if method == "eth_getBlockByNumber":
            return {"number": hex(12)}
        if method == "eth_blockNumber":
            return hex(12)
        bounds = params[0]
        lo, hi = int(bounds["fromBlock"], 16), int(bounds["toBlock"], 16)
        assert hi - lo < 3  # Simulate an RPC provider rejecting larger ranges.
        ranges.append((lo, hi))
        return (
            [
                {
                    "topics": [USED, "0x" + account.address[2:].lower().zfill(64)],
                    "data": payload.payload["authorization"]["nonce"],
                    "transactionHash": "0xpaid",
                }
            ]
            if lo <= 8 <= hi
            else []
        )

    chain.rpc = rpc
    assert await chain.find_transaction(payload, "0x0") is None
    assert ranges == [(0, 2), (3, 5)]
    assert await chain.find_transaction(payload, "0x0") == "0xpaid"
    assert ranges == [(0, 2), (3, 5), (6, 8)]
    await chain.client.aclose()


@pytest.mark.asyncio
async def test_scan_rescans_unfinalized_tail_after_reorg(system: Any) -> None:
    from hosted.job_safe_search.payments import USED, ChainEvidence, decode

    service, account, _ = system
    payload = decode(payment(service, account))
    chain = ChainEvidence("https://rpc.example.invalid", service.requirements)
    chain.log_block_span = 3
    ranges = []
    observed = False

    async def rpc(method: str, params: list[Any]) -> Any:
        if method == "eth_chainId":
            return hex(84532)
        if method == "eth_getBlockByNumber":
            return {"number": hex(2)}
        if method == "eth_blockNumber":
            return hex(5)
        bounds = params[0]
        lo, hi = int(bounds["fromBlock"], 16), int(bounds["toBlock"], 16)
        ranges.append((lo, hi))
        if observed and lo <= 4 <= hi:
            return [
                {
                    "topics": [USED, "0x" + account.address[2:].lower().zfill(64)],
                    "data": payload.payload["authorization"]["nonce"],
                    "transactionHash": "0xlate",
                }
            ]
        return []

    chain.rpc = rpc
    assert await chain.find_transaction(payload, "0x0") is None
    observed = True
    assert await chain.find_transaction(payload, "0x0") == "0xlate"
    assert ranges == [(0, 2), (3, 5), (3, 5)]
    await chain.client.aclose()


@pytest.mark.parametrize("change", ["network", "pay_to", "token_secret"])
def test_ledger_rejects_changed_payment_domain_or_capability_key(system: Any, change: str) -> None:
    service, _, _ = system
    updates: dict[str, Any] = {
        "network": {
            "network": "eip155:8453",
            "mainnet_approved": True,
            "supplier_rights_confirmed": True,
            "realized_payment_fee": None,
        },
        "pay_to": {"pay_to": Account.create().address},
        "token_secret": {"token_secret": b"new-key" * 8},
    }[change]
    with pytest.raises(ValueError, match="deployment_identity"):
        SearchService(
            replace(service.config, **updates),
            Store(service.store.path),
            service.facilitator,
            service.chain,
            service.supplier,
        )
    assert service.facilitator.settles == 0 and service.supplier.calls == 0


@pytest.mark.asyncio
async def test_ledger_identity_survives_restart_and_preserves_free_replay(system: Any) -> None:
    service, account, _ = system
    signature = payment(service, account)
    await service.handle(body(), signature)
    restarted = SearchService(
        service.config,
        Store(service.store.path),
        service.facilitator,
        service.chain,
        service.supplier,
    )
    response = await restarted.handle(body(), signature)
    assert response.status_code == 200 and result(response)["receipt"]["charged_usd"] == "0"
    assert service.facilitator.settles == service.supplier.calls == 1
    assert financial_state(row(restarted))["realized_margin"] == "0.008"


@pytest.mark.asyncio
async def test_legacy_ledger_domain_checked_before_binding(system: Any) -> None:
    service, account, _ = system
    await service.handle(body(), payment(service, account))
    # Simulate a schema before deployment identity was introduced.
    with service.store.connect() as c:
        c.execute("DROP TRIGGER deployment_identity_no_delete")
        c.execute("DELETE FROM deployment_identity")
    with pytest.raises(ValueError, match="legacy_database"):
        SearchService(
            replace(service.config, pay_to=Account.create().address),
            Store(service.store.path),
            service.facilitator,
            service.chain,
            service.supplier,
        )
    service.store.bind_deployment(service.requirements, service.config.token_secret)
    assert financial_state(row(service))["realized_margin"] == "0.008"


def test_deployment_identity_is_append_only(system: Any) -> None:
    import sqlite3

    service, _, _ = system
    with service.store.connect() as c:
        for sql in (
            "DELETE FROM deployment_identity",
            "UPDATE deployment_identity SET token_digest='x'",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                c.execute(sql)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX server process lock")
def test_one_service_writer_across_processes(system: Any) -> None:
    import subprocess

    service, _, _ = system
    child = """from pathlib import Path
import sys
from hosted.job_safe_search.store import Store
try:
    with Store(Path(sys.argv[1])).writer_lease():
        print('unsafe')
except RuntimeError:
    print('blocked')
"""
    with service.store.writer_lease():
        result_child = subprocess.run(
            [sys.executable, "-c", child, str(service.store.path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result_child.returncode == 0 and result_child.stdout.strip() == "blocked"
    with Store(service.store.path).writer_lease():
        assert service.facilitator.settles == 0 and service.supplier.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["exa", "serpex"])
@pytest.mark.parametrize("failure", ["bad_url", "empty", "http_error"])
async def test_adapter_preserves_known_billing_on_unusable_output(
    system: Any, provider: str, failure: str
) -> None:
    from hosted.job_safe_search.supplier import ExaSearch, SerpexSearch

    service, account, _ = system
    supplier = ExaSearch("fixture-key") if provider == "exa" else SerpexSearch("fixture-key", 800)
    await supplier.client.aclose()
    hits = (
        []
        if failure == "empty"
        else [
            {
                "title": "Result",
                "url": "javascript:bad" if failure == "bad_url" else "https://example.com",
            }
        ]
    )
    supplier.client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                503 if failure == "http_error" else 200,
                json={
                    "results": hits,
                    "requestId": "bill-1",
                    "id": "bill-1",
                    "costDollars": {"total": 0.007},
                    "metadata": {"credits_used": 1},
                },
            )
        )
    )
    service.supplier = supplier
    await service.handle(body(), payment(service, account))
    actual = row(service)
    assert actual["supplier_cogs"] == (7000 if provider == "exa" else 800)
    assert actual["supplier_reference"] == "bill-1"
    assert actual["state"] == "SERVICE_FAILED" and actual["liability"] == 15000
    assert financial_state(actual)["realized_margin"] is None
    await supplier.client.aclose()


@pytest.mark.asyncio
async def test_supplier_overrun_blocks_new_payment_after_restart(system: Any) -> None:
    service, account, chain = system
    service.supplier.failure = "cost_overrun"
    await service.handle(body(), payment(service, account))
    assert row(service)["supplier_cogs"] == 8000
    restarted = SearchService(
        service.config, Store(service.store.path), service.facilitator, service.chain, Supplier()
    )
    assert restarted.store.supplier_blocked("test")
    with pytest.raises(Refused, match="supplier_cost_contract_breached"):
        await restarted.handle(body("two"), payment(restarted, account))
    assert service.facilitator.settles == 1 and restarted.supplier.calls == 0
    assert chain.balance_of(service.config.pay_to) == 15000
    assert financial_state(row(service))["realized_margin"] is None


@pytest.mark.parametrize("provider", ["exa", "serpex"])
def test_supplier_missing_credential_fails_before_acceptance(provider: str) -> None:
    from hosted.job_safe_search.supplier import ExaSearch, SerpexSearch

    with pytest.raises(ValueError, match="supplier_credential_required"):
        ExaSearch(" ") if provider == "exa" else SerpexSearch(" ", 800)


@pytest.mark.asyncio
@pytest.mark.parametrize("credits,expected", [(5, 4000), (True, None), (-1, None)])
async def test_serpex_contract_violation_keeps_actual_billing(
    system: Any, credits: Any, expected: int | None
) -> None:
    from hosted.job_safe_search.supplier import SerpexSearch

    service, account, _ = system
    supplier = SerpexSearch("fixture-key", 800)
    await supplier.client.aclose()
    supplier.client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"metadata": {"credits_used": credits}, "results": [], "id": "violation"}
            )
        )
    )
    service.supplier = supplier
    await service.handle(body(), payment(service, account))
    assert row(service)["supplier_cogs"] == expected
    assert service.store.supplier_blocked("serpex") is (expected is not None)
    assert financial_state(row(service))["realized_margin"] is None
    await supplier.client.aclose()


@pytest.mark.asyncio
async def test_metrics_reconciliation_cannot_split_snapshot(system: Any, monkeypatch: Any) -> None:
    import sqlite3

    from hosted.job_safe_search import metrics

    service, account, _ = system
    await service.handle(body(), payment(service, account))
    original_connect = sqlite3.connect
    changed = False

    def connect(*args: Any, **kwargs: Any) -> Any:
        conn = original_connect(*args, **kwargs)

        def mutate(statement: str) -> None:
            nonlocal changed
            if statement == "SELECT * FROM events" and not changed:
                changed = True
                with original_connect(service.store.path) as writer:
                    writer.execute("UPDATE purchases SET refunds=15000 WHERE id=1")
                    writer.execute(
                        "INSERT INTO events(purchase,kind,details,created) VALUES(1,?,?,0)",
                        (
                            "EXTRA_SETTLED_PAYMENT",
                            json.dumps(
                                {
                                    "payer": account.address.lower(),
                                    "nonce": "extra",
                                    "amount": 15000,
                                }
                            ),
                        ),
                    )

        conn.set_trace_callback(mutate)
        return conn

    monkeypatch.setattr(metrics.sqlite3, "connect", connect)
    snapshot = report(service.store.path, set(), network="eip155:84532")
    assert changed and snapshot["refunds_usd"] == "0"
    assert snapshot["additional_settled_payments"] == 0
    assert snapshot["gross_external_settled_revenue_usd"] == "0.015"
    assert snapshot["realized_external_contribution_margin_usd"] == "0.008"
    assert row(service)["refunds"] == 15000


def test_metrics_missing_ledger_does_not_create_file(tmp_path: Path) -> None:
    import sqlite3

    missing = tmp_path / "missing.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        report(missing, set())
    assert not missing.exists()


@pytest.mark.asyncio
async def test_metrics_proves_distinct_repeat_positive_payer(system: Any) -> None:
    service, account, _ = system
    token = None
    for index in range(3):
        request = SearchRequest(
            query=f"distinct query {index}",
            request_id=str(index),
            job_token=token,
            job_budget_usd="0.045",
        )
        response = await service.handle(request, payment(service, account))
        token = json.loads(response.body)["job_token"]
    with service.store.transaction() as conn:
        conn.execute("UPDATE purchases SET delivered=delivered-90000 WHERE id=1")
    result = report(service.store.path, set(), network="eip155:84532")
    evidence = result["wallet_evidence"][0]
    assert evidence["payer"] == account.address.lower()
    assert evidence["successful_paid_calls"] == evidence["positive_margin_distinct_queries"] == 3
    assert evidence["known_realized_margin_usd"] == "0.024"
    assert evidence["returned_after_24h"] and len(set(evidence["settlement_transactions"])) == 3
    assert result["wallets_with_3_plus_positive_margin_distinct_queries"] == 1
    assert (
        report(service.store.path, {account.address.lower()}, network="eip155:84532")[
            "wallet_evidence"
        ]
        == []
    )

    with service.store.transaction() as conn:
        conn.execute("UPDATE purchases SET supplier_cogs=NULL WHERE id=1")
    unresolved = report(service.store.path, set(), network="eip155:84532")
    assert unresolved["wallets_with_3_plus_positive_margin_distinct_queries"] == 0
    assert unresolved["wallet_evidence"][0]["unresolved_successful_calls"] == 1
    assert unresolved["realized_external_contribution_margin_usd"] is None


@pytest.mark.asyncio
async def test_cache_expiry_uses_configured_lifetime_in_atomic_reservation(system: Any) -> None:
    import time

    service, account, _ = system
    service.config = replace(service.config, cache_ttl=10)
    first = result(await service.handle(body(), payment(service, account)))
    with service.store.transaction() as conn:
        conn.execute("UPDATE purchases SET delivered=? WHERE id=1", (time.time() - 20,))
    request = SearchRequest(query="machine budgets", request_id="two", job_token=first["job_token"])
    response = result(await service.handle(request, payment(service, account)))
    assert response["receipt"]["charged_usd"] == "0.015"
    assert not response["receipt"]["cache_hit"]
    assert service.supplier.calls == service.facilitator.settles == 2


@pytest.mark.asyncio
async def test_discovery_examples_match_price_provider_budget_and_cache(system: Any) -> None:
    service, _, _ = system
    configured = SearchService(
        replace(service.config, price=20000, cache_ttl=60),
        service.store,
        service.facilitator,
        service.chain,
        service.supplier,
    )
    challenge = result(configured.challenge())
    example = challenge["extensions"]["bazaar"]["info"]["output"]["example"]
    request_example = challenge["extensions"]["bazaar"]["info"]["input"]["body"]
    assert example["receipt"]["charged_usd"] == "0.02"
    assert example["receipt"]["provider"] == "test"
    assert example["receipt"]["remaining_job_budget_usd"] == "0.04"
    assert request_example["job_budget_usd"] == "0.06"
    assert "60-second" in challenge["resource"]["description"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(configured)),
        base_url="https://search.example.com",
    ) as client:
        manifest = (await client.get("/.well-known/x402.json")).json()
    assert manifest["output_example"] == example and manifest["cache_ttl_seconds"] == 60


@pytest.mark.parametrize("changes", [{"cache_ttl": -1}, {"job_ttl": 0}])
def test_invalid_cache_and_job_lifetimes_fail_closed(system: Any, changes: dict[str, int]) -> None:
    service, _, _ = system
    with pytest.raises(ValueError, match="invalid_cache_or_job_lifetime"):
        SearchService(
            replace(service.config, **changes),
            service.store,
            service.facilitator,
            service.chain,
            service.supplier,
        )


@pytest.mark.asyncio
async def test_proxy_settled_rejected_input_is_recorded_as_liability(system: Any) -> None:
    from hosted.job_safe_search.payments import decode

    service, account, chain = system
    service.config = replace(service.config, recovery_from_block="0x0")
    signed = payment(service, account)
    await service.facilitator.inner.settle(decode(signed), service.requirements)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)),
        base_url="https://search.example.com",
    ) as client:
        for _ in range(2):
            response = await client.post(
                "/search",
                json={"query": "missing request id"},
                headers={"payment-signature": signed},
            )
            assert response.status_code == 422
            assert response.json()["charged_usd"] == "0.015"
    metrics = report(service.store.path, set(), network="eip155:84532")
    assert metrics["gross_external_settled_revenue_usd"] == "0.015"
    assert metrics["unfulfilled_settled_payments"] == 1
    assert metrics["additional_payment_liability_usd"] == "0.015"
    assert metrics["realized_external_contribution_margin_usd"] is None
    assert service.supplier.calls == service.facilitator.settles == 0
    assert chain.balance_of(service.config.pay_to) == 15000
    assert (
        report(service.store.path, {account.address.lower()}, network="eip155:84532")[
            "gross_external_settled_revenue_usd"
        ]
        == "0"
    )


@pytest.mark.asyncio
async def test_replay_rpc_failure_persists_signed_observation(system: Any) -> None:
    from hosted.job_safe_search.payments import decode

    service, account, chain = system
    service.config = replace(service.config, recovery_from_block="0x0")
    await service.handle(body(), payment(service, account))
    fresh = payment(service, account)
    await service.facilitator.inner.settle(decode(fresh), service.requirements)
    original = service.chain.find_transaction

    async def unavailable(*args: Any) -> Any:
        raise RuntimeError("RPC unavailable")

    service.chain.find_transaction = unavailable
    response = result(await service.handle(body(), fresh))
    assert response["receipt"]["financial_state"] == "UNRESOLVED"
    assert len(service.store.observed_payments()) == 1
    assert financial_state(row(service))["realized_margin"] is None
    service.chain.find_transaction = original
    restarted = SearchService(
        service.config,
        Store(service.store.path),
        service.facilitator,
        service.chain,
        service.supplier,
    )
    await restarted.recover("0x0")
    assert restarted.store.observed_payments() == []
    metrics = report(service.store.path, set(), network="eip155:84532")
    assert metrics["additional_settled_payments"] == 1
    assert metrics["gross_external_settled_revenue_usd"] == "0.03"
    assert metrics["additional_payment_liability_usd"] == "0.015"
    assert metrics["realized_external_contribution_margin_usd"] is None
    assert service.supplier.calls == service.facilitator.settles == 1
    assert chain.balance_of(service.config.pay_to) == 30000


@pytest.mark.asyncio
async def test_rejected_signed_payment_stays_unknown_until_absence_proven(system: Any) -> None:
    service, account, _ = system
    service.config = replace(service.config, recovery_from_block="0x0")
    signed = payment(service, account)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)),
        base_url="https://search.example.com",
    ) as client:
        response = await client.post("/search", json={}, headers={"payment-signature": signed})
        assert response.json()["charged_usd"] is None
    assert (
        report(service.store.path, set(), network="eip155:84532")[
            "realized_external_contribution_margin_usd"
        ]
        is None
    )

    async def proven(payload: Any) -> bool:
        return True

    service.chain.settlement_impossible = proven
    await service.recover("0x0")
    assert (
        report(service.store.path, set(), network="eip155:84532")[
            "realized_external_contribution_margin_usd"
        ]
        == "0"
    )
    assert service.facilitator.settles == service.supplier.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expired,spent,finalized,expected",
    [
        (False, False, True, False),
        (True, True, True, False),
        (True, False, False, False),
        (True, False, True, True),
    ],
)
async def test_absence_proof_requires_finalized_expiry_and_unspent_nonce(
    system: Any, expired: bool, spent: bool, finalized: bool, expected: bool
) -> None:
    from hosted.job_safe_search.payments import ChainEvidence, decode

    service, account, _ = system
    payload = decode(payment(service, account))
    evidence = ChainEvidence("https://rpc.example.com", service.requirements, finalized=finalized)

    async def rpc(method: str, params: Any) -> Any:
        if method == "eth_chainId":
            return hex(84532)
        if method == "eth_getBlockByNumber":
            return {
                "number": "0x10",
                "timestamp": hex(
                    int(payload.payload["authorization"]["validBefore"]) + (1 if expired else -1)
                ),
            }
        assert method == "eth_call" and params[1] == "0x10"
        assert params[0]["to"] == service.requirements.asset
        return "0x" + ("1" if spent else "0").zfill(64)

    evidence.rpc = rpc
    assert await evidence.settlement_impossible(payload) is expected
    await evidence.client.aclose()


@pytest.mark.asyncio
async def test_observed_proxy_payment_cannot_be_reused_as_new_purchase(system: Any) -> None:
    from hosted.job_safe_search.payments import decode

    service, account, _ = system
    service.config = replace(service.config, recovery_from_block="0x0")
    original = payment(service, account)
    first = result(await service.handle(body(), original))
    extra = payment(service, account)
    await service.facilitator.inner.settle(decode(extra), service.requirements)
    await service.handle(body(), extra)
    request = SearchRequest(query="new query", request_id="two", job_token=first["job_token"])
    with pytest.raises(Refused, match="payment_observed_requires_reconciliation"):
        await service.handle(request, extra)
    assert service.supplier.calls == service.facilitator.settles == 1
    assert (
        report(service.store.path, set(), network="eip155:84532")[
            "gross_external_settled_revenue_usd"
        ]
        == "0.03"
    )


@pytest.mark.asyncio
async def test_unfulfilled_refund_preserves_realized_loss(system: Any) -> None:
    from hosted.job_safe_search.payments import decode

    service, account, chain = system
    service.config = replace(service.config, recovery_from_block="0x0")
    signed = payment(service, account)
    payload = decode(signed)
    await service.facilitator.inner.settle(payload, service.requirements)
    await service.observe_payment(signed)
    nonce = payload.payload["authorization"]["nonce"]
    with pytest.raises(ValueError):
        service.store.reconcile_unfulfilled_refund(
            account.address,
            nonce,
            refund_transaction="",
            variable_fees=100,
            evidence="missing transaction",
        )
    service.store.reconcile_unfulfilled_refund(
        account.address,
        nonce,
        refund_transaction="independently-confirmed-test-refund",
        variable_fees=100,
        evidence="controlled test ledger only; no onchain refund submitted",
    )
    metrics = report(service.store.path, set(), network="eip155:84532")
    assert metrics["gross_external_settled_revenue_usd"] == metrics["refunds_usd"] == "0.015"
    assert metrics["variable_payment_fees_usd"] == "0.0001"
    assert metrics["realized_external_contribution_margin_usd"] == "-0.0001"
    assert metrics["negative_margin_requests"] == 1
    assert metrics["unresolved_transactions"] == 0
    assert service.supplier.calls == service.facilitator.settles == 0
    assert chain.balance_of(service.config.pay_to) == 15000


@pytest.mark.asyncio
async def test_incomplete_proxy_scan_does_not_claim_no_payment(system: Any) -> None:
    from hosted.job_safe_search.payments import decode

    service, account, _ = system
    service.config = replace(service.config, recovery_from_block="0x0")
    signed = payment(service, account)
    await service.facilitator.inner.settle(decode(signed), service.requirements)
    original = service.chain.find_transaction

    async def incomplete(*args: Any) -> Any:
        return None

    service.chain.find_transaction = incomplete
    await service.handle(body(), signed)
    assert row(service)["state"] == "PAYMENT_UNKNOWN"
    assert financial_state(row(service))["settled_revenue"] is None
    assert service.supplier.calls == service.facilitator.settles == 0
    service.chain.find_transaction = original
    await service.recover("0x0")
    assert row(service)["state"] == "DELIVERED"
    assert service.supplier.calls == 1 and service.facilitator.settles == 0


@pytest.mark.asyncio
async def test_risk_ceiling_uses_actual_known_overrun_on_provider_change(system: Any) -> None:
    service, account, _ = system
    service.supplier.failure = "empty"
    first = result(await service.handle(body(), payment(service, account)))
    with service.store.transaction() as conn:
        conn.execute("UPDATE purchases SET supplier_cogs=20000000 WHERE id=1")
    service.supplier = Supplier()
    service.supplier.name = "replacement"
    request = SearchRequest(query="new query", request_id="two", job_token=first["job_token"])
    with pytest.raises(Refused, match="unresolved_risk_ceiling"):
        await service.handle(request, payment(service, account))
    assert service.supplier.calls == 0 and service.facilitator.settles == 1


def test_mainnet_accepts_bounded_unknown_fee_without_recognizing_estimate(system: Any) -> None:
    service, _, _ = system
    config = replace(
        service.config,
        network="eip155:8453",
        mainnet_approved=True,
        supplier_rights_confirmed=True,
        realized_payment_fee=None,
    )
    config.validate(Supplier())
    for assumed_fee in (0, 1000):
        with pytest.raises(ValueError, match="per_purchase_reconciliation"):
            replace(config, realized_payment_fee=assumed_fee).validate(Supplier())


@pytest.mark.asyncio
@pytest.mark.parametrize("actual_fee,expected_margin", [(0, "0.008"), (1000, "0.007")])
async def test_fee_tier_changes_require_actual_billing_evidence(
    system: Any, actual_fee: int, expected_margin: str
) -> None:
    service, account, _ = system
    service = SearchService(
        replace(service.config, realized_payment_fee=None, risk_ceiling=46000),
        service.store,
        service.facilitator,
        service.chain,
        service.supplier,
    )
    response = await service.handle(body(), payment(service, account))
    assert response.status_code == 200
    state = financial_state(row(service))
    assert state["settled_revenue"] == "0.015"
    assert state["supplier_cogs"] == "0.007"
    assert state["variable_fees"] is None and state["realized_margin"] is None
    assert not state["resolved"]
    # Unknown billing stays reserved, even after useful output is delivered.
    with pytest.raises(Refused, match="risk_ceiling"):
        service.store.reserve(
            payer=account.address.lower(),
            job="different-job",
            request="different-request",
            fingerprint="different-query",
            body=body().model_dump_json(),
            authenticated=False,
            nonce="unused-nonce",
            payload="{}",
            expires=0,
            price=15000,
            supplier_bound=7000,
            fee_bound=1000,
            provider="test",
            risk_ceiling=45000,
            budget=None,
        )
    assert service.store.resolve_financials(
        1,
        supplier_cogs=7000,
        variable_fees=actual_fee,
        evidence=f"actual facilitator invoice allocated to transaction; fee={actual_fee}",
    )
    resolved = financial_state(row(service))
    assert resolved["variable_fees"] == ("0" if actual_fee == 0 else "0.001")
    assert resolved["realized_margin"] == expected_margin and resolved["resolved"]
    assert service.supplier.calls == service.facilitator.settles == 1


@pytest.mark.asyncio
async def test_known_fee_overrun_blocks_new_payment_after_restart(system: Any) -> None:
    service, account, _ = system
    original = payment(service, account)
    await service.handle(body(), original)
    service.store.resolve_financials(
        1,
        supplier_cogs=7000,
        variable_fees=9000,
        evidence="actual invoice including taxes",
    )
    assert financial_state(row(service))["realized_margin"] == "-0.001"
    restarted = SearchService(
        service.config,
        Store(service.store.path),
        service.facilitator,
        service.chain,
        service.supplier,
    )
    with pytest.raises(Refused, match="payment_fee_bound_breached"):
        await restarted.handle(
            body(request_id="another").model_copy(update={"query": "different query"}),
            payment(restarted, account),
        )
    assert restarted.facilitator.settles == restarted.supplier.calls == 1
    assert (await restarted.handle(body(), original)).status_code == 200


@pytest.mark.asyncio
async def test_fee_ceiling_increase_freezes_old_authority_before_supplier(system: Any) -> None:
    service, account, _ = system
    service.chain.final = False
    await service.handle(body(), payment(service, account))
    assert row(service)["state"] == "FINALITY_PENDING"
    service.chain.final = True
    updated = SearchService(
        replace(service.config, fee_bound=2000),
        Store(service.store.path),
        service.facilitator,
        service.chain,
        service.supplier,
    )
    await updated.advance(1)
    assert row(updated)["state"] == "FINALITY_PENDING"
    assert updated.supplier.calls == 0
    assert financial_state(row(updated))["realized_margin"] is None
