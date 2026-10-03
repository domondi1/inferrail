"""One job, one budget, everything it buys.

    python examples/agent_economy/demo.py              # local: real x402, simulated chain
    python examples/agent_economy/demo.py --testnet    # Base Sepolia, public x402.org facilitator

What runs:
  seller      a stock x402 server (paid search $0.002, paid chat $0.003); no Inferrail code
  authority   the self-hosted economic authority; the ONLY process holding the payer key
  agents      parent + sub-agent processes holding an authority token and nothing else

Testnet mode needs PAYER_PRIVATE_KEY (a Base Sepolia wallet funded with a few
cents of test USDC from https://faucet.circle.com) and SELLER_PAY_TO. It moves
test USDC only. There is no mainnet path.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from eth_account import Account
from fastapi import FastAPI, Header, HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent))
from authority import AuthorityRuntime, ModelProvider, Refused, estimate_prompt_tokens  # noqa: E402
from local_chain import LocalFacilitator, LocalUsdcChain  # noqa: E402
from seller import create_seller_app  # noqa: E402

HERE = Path(__file__).resolve().parent
WORK_ID = "research-run-42"
BUDGET = Decimal("0.020")
USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"


class BaseSepoliaReader:
    """Reads EIP-3009 state straight from the USDC contract. No facilitator API needed."""

    RPC = "https://sepolia.base.org"

    def _rpc(self, method: str, params: list[Any]) -> Any:
        reply = httpx.post(
            self.RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=30,
        ).json()
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error']}")
        return reply["result"]

    def authorization_state(self, authorizer: str, nonce: str) -> bool:
        """Authoritative: has this (payer, nonce) authorization been used?"""
        from eth_abi import decode, encode
        from eth_utils import keccak

        data = keccak(text="authorizationState(address,bytes32)")[:4] + encode(
            ["address", "bytes32"], [authorizer, bytes.fromhex(nonce.removeprefix("0x"))]
        )
        result = self._rpc("eth_call", [{"to": USDC, "data": "0x" + data.hex()}, "latest"])
        return bool(decode(["bool"], bytes.fromhex(result[2:]))[0])

    def used_event(self, authorizer: str, nonce: str, windows: int = 12) -> Any:
        """Best effort: the transaction hash, from AuthorizationUsed logs.

        The public RPC limits eth_getLogs to 1,000 blocks (about 33 minutes on
        Base), so this scans backwards window by window. `authorization_state`
        stays the authoritative answer if the event is older than the scan.
        """
        from eth_utils import keccak

        topics = [
            "0x" + keccak(text="AuthorizationUsed(address,bytes32)").hex(),
            "0x" + authorizer.lower().removeprefix("0x").rjust(64, "0"),
            nonce.lower(),
        ]
        to_block = int(self._rpc("eth_blockNumber", []), 16)
        for _ in range(windows):
            from_block = max(0, to_block - 999)
            logs = self._rpc(
                "eth_getLogs",
                [
                    {
                        "address": USDC,
                        "fromBlock": hex(from_block),
                        "toBlock": hex(to_block),
                        "topics": topics,
                    }
                ],
            )
            if logs:
                return type("Event", (), {"tx": logs[0]["transactionHash"]})()
            if from_block == 0:
                break
            to_block = from_block - 1
        return None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def serve(app: FastAPI, port: int) -> None:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)


def stub_provider() -> ModelProvider:
    """Stands in for a provider call made with a key the runtime holds (the gateway's job)."""

    def complete(messages: list[dict[str, str]], max_tokens: int) -> tuple[str, int, int]:
        return (
            "1) search prior work 2) summarize 3) cite",
            estimate_prompt_tokens(messages),
            min(max_tokens, 60),
        )

    return ModelProvider(
        complete, Decimal("0.000001"), Decimal("0.000004"), name="model-provider (stub)"
    )


def authority_app(rt: AuthorityRuntime) -> FastAPI:
    app = FastAPI()

    def token(authorization: str | None) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401)
        return authorization.removeprefix("Bearer ")

    def guarded(fn: Any) -> Any:
        try:
            return fn()
        except Refused as r:
            raise HTTPException(
                402, detail={"state": "REFUSED", "reason": r.reason, "detail": r.detail}
            ) from None
        except PermissionError:
            raise HTTPException(403) from None

    @app.post("/model")
    def model(body: dict[str, Any], authorization: str | None = Header(None)) -> Any:
        return guarded(
            lambda: rt.model_call(token(authorization), body["messages"], int(body["max_tokens"]))
        )

    @app.post("/pay")
    def pay(body: dict[str, Any], authorization: str | None = Header(None)) -> Any:
        extra = {k: body[k] for k in ("params", "json") if k in body}
        return guarded(lambda: rt.pay(token(authorization), body["method"], body["url"], **extra))

    @app.post("/delegate")
    def delegate(body: dict[str, Any], authorization: str | None = Header(None)) -> Any:
        return guarded(
            lambda: {
                "token": rt.delegate(
                    token(authorization), body["agent_id"], Decimal(body["max_usd"])
                )
            }
        )

    @app.post("/finish")
    def finish(authorization: str | None = Header(None)) -> Any:
        return guarded(lambda: rt.finish(token(authorization)) or {"finished": True})

    @app.exception_handler(HTTPException)
    async def flat(_: Any, exc: HTTPException) -> Any:
        from fastapi.responses import JSONResponse

        body = exc.detail if isinstance(exc.detail, dict) else {"error": exc.detail}
        return JSONResponse(body, status_code=exc.status_code)

    return app


def usd(value: Any) -> str:
    return f"${Decimal(value or 0).quantize(Decimal('0.000001')).normalize():f}"


def print_record(record: dict[str, Any], chain_note: str) -> None:
    root = record["authority"]
    print("\n=== Economic record:", record["work_id"], "===")
    print(f"original authority      {usd(root['authority_usd'])}")
    for d in root.get("delegations", []):
        print(
            f"delegated to {d['agent_id']:<11} {usd(d['authority_usd'])}  "
            f"spent {usd(d['consumed_usd'])}  "
            f"returned to parent {usd(d['returned_to_parent_usd'])}"
        )
    print(f"settled spend (total)   {usd(record['settled_spend_usd'])}")
    print(f"still reserved          {usd(root['reserved_for_children_usd'])}")
    print(f"remaining authority     {usd(root['remaining_usd'])}")
    print(f"ledger invariant        {root['invariant']}")
    print("\nactions:")
    for a in record["actions"]:
        proof = (
            f"tx {a['tx'][:18]}…  on-chain used={a.get('chain_authorization_used')}"
            if a["tx"]
            else "provider-reported usage"
        )
        print(
            f"  {a['agent_id']:<10} {a['kind']:<5} {usd(a['actual_usd']):<9} "
            f"{a['state']:<8} {a['resource'][:46]:<46} {proof}"
        )
    print(
        "\nrefused BEFORE any payment was authorized:",
        len(record["refused_before_authorization"]),
    )
    for a in record["refused_before_authorization"]:
        print(f"  {a['agent_id']:<10} wanted {usd(a['amount_usd'])} for {a['resource'][:46]}")
    print(f"\npayment proof: {chain_note}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--testnet", action="store_true")
    ap.add_argument("--json", type=Path, help="also write the full record here")
    args = ap.parse_args()

    seller_port, authority_port = free_port(), free_port()
    seller_url = f"http://127.0.0.1:{seller_port}"
    if args.testnet:
        from x402.http import FacilitatorConfig, HTTPFacilitatorClient

        payer = Account.from_key(os.environ["PAYER_PRIVATE_KEY"])
        facilitator = HTTPFacilitatorClient(FacilitatorConfig(url="https://x402.org/facilitator"))
        chain: Any = BaseSepoliaReader()
        pay_to = os.environ["SELLER_PAY_TO"]
        chain_note = "Base Sepolia: check each tx on https://sepolia.basescan.org"
    else:
        payer = Account.create()
        local = LocalUsdcChain()
        local.mint(
            payer.address, 20_000
        )  # fund the payer with exactly the budget: the hard backstop
        facilitator, chain, pay_to = LocalFacilitator(local), local, Account.create().address
        chain_note = (
            "LOCAL simulated chain (signatures and x402 are real; the ledger of USDC is not)"
        )

    serve(create_seller_app(facilitator, pay_to, seller_url), seller_port)
    state = Path(tempfile.mkdtemp(prefix="inferrail-authority-"))
    rt = AuthorityRuntime(
        state, payer=payer, chain=chain, http=httpx.Client(timeout=60), provider=stub_provider()
    )
    serve(authority_app(rt), authority_port)
    parent_token = rt.open_work(WORK_ID, BUDGET, agent_id="parent")

    print(
        f"Job {WORK_ID}: authority ${BUDGET}. Payer key lives only in the authority runtime.\n",
        flush=True,
    )
    agent_env = {
        "PATH": os.environ["PATH"],
        "AUTHORITY_URL": f"http://127.0.0.1:{authority_port}",
        "AUTHORITY_TOKEN": parent_token,
        "SELLER_URL": seller_url,
        "ROLE": "parent",
    }
    print("agent process environment:", sorted(agent_env), "(no key of any kind)\n", flush=True)
    subprocess.run([sys.executable, str(HERE / "agent.py")], env=agent_env, check=True)

    rt.reconcile()
    record = rt.record(WORK_ID)
    print_record(record, chain_note)
    if not args.testnet:
        outflow = 20_000 - local.balance_of(payer.address)
        print(
            f"payer USDC outflow: {usd(Decimal(outflow).scaleb(-6))} "
            f"(= x402 settled spend; can never exceed the ${BUDGET} funded)"
        )
    if args.json:
        args.json.write_text(json.dumps(record, indent=2, default=str))


if __name__ == "__main__":
    main()
