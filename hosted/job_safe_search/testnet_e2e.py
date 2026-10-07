"""Opt-in, real Base Sepolia HTTP/process lifecycle. Never accepts mainnet keys/networks.

Run with a NEW state directory and three funded Sepolia-only EOA test wallets.
Wallet JSON: {"buyer": {"address": "0x...", "key": "..."}, "merchant": {...},
"supplier": {...}}. Keep this file outside the repository, mode 0600.
All wallets are controlled tests and MUST be excluded from external metrics.
No faucet, account creation, mainnet funding, or bootstrap payment occurs here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing
import os
from pathlib import Path
from typing import Any

import httpx
from cdp.x402 import create_facilitator_config
from eth_account import Account
from x402.http import HTTPFacilitatorClient
from x402.mechanisms.evm.exact.client import ExactEvmScheme
from x402.schemas import PaymentPayload, PaymentRequirements, ResourceInfo

from .contract import SearchRequest
from .economics import financial_state
from .metrics import report
from .payments import ChainEvidence, decode, encode
from .service import Config, SearchService, create_app
from .store import Store
from .supplier import FixtureSearch, SupplierResult

NETWORK = "eip155:84532"
RPC = "https://sepolia.base.org"


def sign(challenge: dict[str, Any], key: str) -> str:
    req = PaymentRequirements.model_validate(challenge["accepts"][0])
    if str(req.network) != NETWORK:
        raise ValueError("testnet_only")
    account = Account.from_key(key)
    data = ExactEvmScheme(account).create_payment_payload(req)
    return encode(
        PaymentPayload(
            x402_version=2,
            payload=data,
            accepted=req,
            resource=ResourceInfo(url=challenge["resource"]["url"]),
        ).model_dump(by_alias=True)
    )


class PaidFixture:
    name = "paid_sepolia_fixture"
    max_cost = 7000

    def __init__(self, wallets: dict[str, Any], root: Path, mode: str):
        self.wallets, self.root, self.mode = wallets, root, mode
        self.client = httpx.AsyncClient(timeout=120)

    async def search(self, request: SearchRequest) -> SupplierResult:
        challenge = (await self.client.post("http://127.0.0.1:18423/search", json={})).json()
        signature = sign(challenge, self.wallets["merchant"]["key"])
        # Durable outgoing authorization before network dispatch; never generate a replacement.
        with (self.root / "outbound-signature.json").open("x") as saved:
            os.chmod(saved.name, 0o600)
            json.dump({"signature": signature, "request_id": request.request_id}, saved)
        response = await self.client.post(
            "http://127.0.0.1:18423/search",
            json={"query": request.query, "request_id": request.request_id},
            headers={"PAYMENT-SIGNATURE": signature},
        )
        if response.status_code != 200:
            raise RuntimeError("supplier_settlement_or_delivery_uncertain")
        data = response.json()
        (self.root / "outbound-receipt.json").write_text(json.dumps(data["receipt"]))
        if self.mode == "supplier_crash":
            os._exit(82)
        return SupplierResult(data["results"], 7000, data["receipt"]["transaction"])


class CrashAfterSettlement:
    def __init__(self, inner: Any, mode: str):
        self.inner, self.mode = inner, mode

    async def verify(self, *args: Any) -> Any:
        return await self.inner.verify(*args)

    async def settle(self, *args: Any) -> Any:
        result = await self.inner.settle(*args)
        if self.mode == "settlement_crash" and result.success:
            os._exit(81)
        return result

    async def aclose(self) -> None:
        await self.inner.aclose()


def serve(wallet_file: str, state: str, role: str, mode: str, from_block: str) -> None:
    import uvicorn

    wallets = json.loads(Path(wallet_file).read_text())
    root = Path(state)
    os.environ["SEARCH_RECOVERY_FROM_BLOCK"] = from_block
    supplier = FixtureSearch() if role == "supplier" else PaidFixture(wallets, root, mode)
    config = Config(
        pay_to=wallets[role]["address"],
        resource_url=f"https://testnet.example.invalid/{role}/search",
        token_secret=(root / "token-secret").read_bytes(),
        network=NETWORK,
        recovery_from_block=from_block,
        price=7000 if role == "supplier" else 15000,
        fee_bound=0,
        minimum_margin=0,
        realized_payment_fee=0,
    )
    facilitator = CrashAfterSettlement(HTTPFacilitatorClient(create_facilitator_config()), mode)
    service = SearchService(
        config,
        Store(root / f"{role}.sqlite"),
        facilitator,
        ChainEvidence(RPC, config.requirements(), finalized=False),
        supplier,
    )
    # Nonpublic fixtures must never seed the production discovery catalogue.
    service.extensions = {}
    uvicorn.run(
        create_app(service),
        host="127.0.0.1",
        port=18423 if role == "supplier" else 18422,
        log_level="warning",
    )


async def wait_health(client: httpx.AsyncClient, port: int) -> None:
    for _ in range(120):
        try:
            response = await client.get(f"http://127.0.0.1:{port}/health")
            if response.status_code == 200:
                assert response.json()["network"] == NETWORK
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.25)
    raise RuntimeError("testnet_process_did_not_start")


async def exercise(wallet_file: Path, state: Path, scenarios: list[str] | None = None) -> None:
    if state.exists():
        raise ValueError("new_state_directory_required; inspect existing evidence before any retry")
    state.mkdir(mode=0o700)
    wallets = json.loads(wallet_file.read_text())
    excluded = {v["address"].lower() for v in wallets.values()}
    for wallet in wallets.values():
        if Account.from_key(wallet["key"]).address.lower() != wallet["address"].lower():
            raise ValueError("wallet_key_address_mismatch")
    context = multiprocessing.get_context("spawn")
    evidence: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            RPC, json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}
        )
        from_block = response.json()["result"]
        for mode in scenarios or ["success", "settlement_crash", "supplier_crash", "pre_settled"]:
            root = state / mode
            root.mkdir()
            (root / "token-secret").write_bytes(os.urandom(48))
            os.chmod(root / "token-secret", 0o600)

            def start(role: str, behavior: str, root: Path = root) -> Any:
                proc = context.Process(
                    target=serve, args=(str(wallet_file), str(root), role, behavior, from_block)
                )
                proc.start()
                return proc

            upstream = start("supplier", "success")
            merchant = start("merchant", mode)
            try:
                await wait_health(client, 18423)
                await wait_health(client, 18422)
                challenge = (await client.post("http://127.0.0.1:18422/search", json={})).json()
                signature = sign(challenge, wallets["buyer"]["key"])
                body = {
                    "query": f"deterministic {mode}",
                    "request_id": mode,
                    "job_budget_usd": "0.015",
                }
                headers = {"PAYMENT-SIGNATURE": signature}
                if mode == "pre_settled":
                    facilitator = HTTPFacilitatorClient(create_facilitator_config())
                    payload = decode(signature)
                    settled = await facilitator.settle(payload, payload.accepted)
                    (root / "proxy-settlement.json").write_text(settled.model_dump_json())
                    await facilitator.aclose()
                    if not settled.success:
                        raise RuntimeError(
                            "proxy_payment_uncertain; inspect evidence, never sign again"
                        )
                try:
                    response = await client.post(
                        "http://127.0.0.1:18422/search", json=body, headers=headers
                    )
                    data = response.json()
                except httpx.HTTPError:
                    if mode in ("success", "pre_settled"):
                        raise
                if mode in ("settlement_crash", "supplier_crash"):
                    merchant.join(timeout=5)
                    assert merchant.exitcode == (81 if mode == "settlement_crash" else 82)
                    merchant = start("merchant", "success")
                    await wait_health(client, 18422)
                for _ in range(30):
                    response = await client.post(
                        "http://127.0.0.1:18422/search", json=body, headers=headers
                    )
                    data = response.json()
                    if response.status_code == 200 or mode == "supplier_crash":
                        break
                    await asyncio.sleep(2)
                store = Store(root / "merchant.sqlite")
                row = store.get(1)
                assert row["state"] == (
                    "SUPPLIER_UNKNOWN" if mode == "supplier_crash" else "DELIVERED"
                )
                assert financial_state(row)["realized_margin"] == (
                    None if mode == "supplier_crash" else "0.008"
                )
                # Same request with a fresh signature must never settle again.
                fresh = sign(challenge, wallets["buyer"]["key"])
                await client.post(
                    "http://127.0.0.1:18422/search", json=body, headers={"PAYMENT-SIGNATURE": fresh}
                )
                if mode != "supplier_crash":
                    cache = {
                        "query": body["query"],
                        "request_id": "cache",
                        "job_token": data["job_token"],
                    }
                    cached = (await client.post("http://127.0.0.1:18422/search", json=cache)).json()
                    assert (
                        cached["receipt"]["cache_hit"] and cached["receipt"]["charged_usd"] == "0"
                    )
                    refused = await client.post(
                        "http://127.0.0.1:18422/search",
                        json={**cache, "query": "distinct query", "request_id": "over-budget"},
                    )
                    assert (
                        refused.status_code == 409
                        and refused.json()["error"] == "job_budget_exhausted"
                    )
                    immutable = await client.post(
                        "http://127.0.0.1:18422/search", json={**cache, "job_budget_usd": "0.03"}
                    )
                    assert immutable.status_code == 409
                with store.connect() as conn:
                    assert conn.execute("SELECT COUNT(*) FROM purchases").fetchone()[0] == 1
                supplier_row = Store(root / "supplier.sqlite").get(1)
                with Store(root / "supplier.sqlite").connect() as conn:
                    assert conn.execute("SELECT COUNT(*) FROM purchases").fetchone()[0] == 1
                metrics = report(store.path, excluded)
                assert metrics["external_paid_calls"] == 0
                assert metrics["gross_external_settled_revenue_usd"] == "0"
                evidence.append(
                    {
                        "scenario": mode,
                        "inbound": row["tx"],
                        "outbound": supplier_row["tx"],
                        "financial_state": financial_state(row),
                        "supplier_state": supplier_row["state"],
                        "metrics": metrics,
                    }
                )
                (state / "evidence.json").write_text(
                    json.dumps(
                        {
                            "network": NETWORK,
                            "confirmation": "canonical mined receipt; not finalized",
                            "controlled": True,
                            "from_block": from_block,
                            "scenarios": evidence,
                        },
                        indent=2,
                    )
                )
                print(json.dumps(evidence[-1]), flush=True)
            finally:
                for proc in (merchant, upstream):
                    if proc.is_alive():
                        proc.terminate()
                    proc.join(timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wallet-file", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--run-actual-testnet", action="store_true", required=True)
    parser.add_argument(
        "--scenarios",
        nargs="+",
        choices=["success", "settlement_crash", "supplier_crash", "pre_settled"],
    )
    args = parser.parse_args()
    if args.wallet_file.stat().st_mode & 0o077:
        raise ValueError("wallet_file_must_be_private_mode_0600")
    asyncio.run(exercise(args.wallet_file.resolve(), args.state_dir.resolve(), args.scenarios))


if __name__ == "__main__":
    main()
