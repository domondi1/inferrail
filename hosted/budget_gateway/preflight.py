"""Pre-deploy check for Inferrail Hosted's payment configuration. Run it before the new process
starts, e.g. as the platform's pre-deploy command:

    python3 hosted/budget_gateway/preflight.py --expect-pay-to 0xYourReceivingAddress

It exits non-zero, and so stops the deploy, when the environment would either crash the service
at startup or collect payments differently from what was approved:
- the settings themselves don't load (e.g. mainnet without BG_X402_MAINNET_APPROVED=1 or
  BG_BASE_RPC_URL): the same check the service runs at startup, run before it can take the
  shared process down;
- a receiving address is set but isn't exactly the expected one (or payments are on and no
  expected address was given);
- mainnet without the facilitator API key, or with a wallet secret present (this service must
  never be able to sign for a wallet);
- the RPC isn't the configured chain, or the facilitator doesn't support `exact` on it.

With payments off (no BG_X402_PAY_TO) it only checks that the settings load. It prints names
and public values only, never secret values.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

CHAIN_IDS = {"eip155:8453": "0x2105", "eip155:84532": "0x14a34"}


def rpc_chain_id(url: str) -> str:
    import httpx

    r = httpx.post(
        url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}, timeout=20
    )
    return str(r.json().get("result"))


def facilitator_kinds(env: dict[str, str]) -> set[tuple[str, str]]:
    """(scheme, network) pairs the configured facilitator supports, fetched with the API key
    (and never the wallet secret)."""
    from cdp.x402 import create_facilitator_config
    from x402.http import HTTPFacilitatorClient

    client = HTTPFacilitatorClient(
        create_facilitator_config(
            api_key_id=env["CDP_API_KEY_ID"], api_key_secret=env["CDP_API_KEY_SECRET"]
        )
    )
    result: Any = client.get_supported()
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    data = result.model_dump() if hasattr(result, "model_dump") else result
    return {(str(k.get("scheme")), str(k.get("network"))) for k in data.get("kinds", [])}


def check(
    env: dict[str, str],
    expect_pay_to: str | None,
    *,
    chain_id: Callable[[str], str] = rpc_chain_id,
    kinds: Callable[[dict[str, str]], set[tuple[str, str]]] = facilitator_kinds,
) -> list[str]:
    """Problems that must stop the deploy (empty = OK). Prints what it verified."""
    import cohost

    problems: list[str] = []
    service = cohost.load_modules()[1]
    try:
        if env.get("BG_MOUNT_PREFIX", cohost.PREFIX) != cohost.PREFIX:
            raise RuntimeError(f"BG_MOUNT_PREFIX must be {cohost.PREFIX} when cohosted")
        settings = service.Settings.from_env({**env, "BG_MOUNT_PREFIX": cohost.PREFIX})
    except Exception as ex:  # exactly what would stop the service at startup
        return [f"settings don't load, so the service would not start: {ex}"]
    if env.get("CDP_WALLET_SECRET"):
        problems.append(
            "CDP_WALLET_SECRET is set: remove it; this service never signs for a wallet"
        )
    pay_to = settings.x402_pay_to
    if not pay_to:
        print("payments: OFF (no BG_X402_PAY_TO)")
        return problems
    network = settings.x402_network
    print(f"payments: ON, network {network}, payTo {pay_to}, pack ${settings.x402_pack_price_usd}")
    if not expect_pay_to:
        problems.append("payments are on but no --expect-pay-to was given to check them against")
    elif pay_to != expect_pay_to:
        problems.append(f"BG_X402_PAY_TO is {pay_to}, not the approved {expect_pay_to}")
    if network == service.MAINNET:
        missing = [k for k in ("CDP_API_KEY_ID", "CDP_API_KEY_SECRET") if not env.get(k)]
        if missing:
            problems.append(f"mainnet needs {', '.join(missing)} (names only are shown)")
            return problems
    if settings.rpc_url:
        try:
            got = chain_id(settings.rpc_url)
            if got != CHAIN_IDS[network]:
                problems.append(f"BG_BASE_RPC_URL is chain {got}, not {network}")
            else:
                print(f"rpc: chain id {got} matches {network}")
        except Exception as ex:
            problems.append(f"BG_BASE_RPC_URL unreachable: {type(ex).__name__}")
    if network == service.MAINNET:
        try:
            if ("exact", network) in kinds(env):
                print(f"facilitator: authenticated; supports exact on {network}")
            else:
                problems.append(f"the facilitator does not support exact on {network}")
        except Exception as ex:
            problems.append(f"facilitator check failed with the API key: {type(ex).__name__}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--expect-pay-to", help="the approved receiving address (EIP-55)")
    args = ap.parse_args(argv)
    problems = check(dict(os.environ), args.expect_pay_to)
    for p in problems:
        print(f"PREFLIGHT FAILED: {p}")
    print("PREFLIGHT OK" if not problems else "deploy stopped; the running version keeps serving")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
