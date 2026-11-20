"""Buy one public web page as clean markdown from Inferrail Web Read, paying with x402.

    EVM_PRIVATE_KEY=0x... python examples/web_read/buy_page.py https://example.com/

The key is your agent's own Base wallet (it needs a little USDC; no ETH, the facilitator pays
gas). The script never pays more than --max-usd per page (default $0.002, the listed price):
the x402 SDK's spend controls refuse a larger offer before anything is signed. You pay only
for a delivered page; refusals and site errors are never charged.

Safe retries: every purchase carries a request_id. If the response is lost, sending the same
url and request_id again from the same wallet returns the paid page without a second charge,
so this script retries once on a network error with the same request_id.

Requires: pip install "x402[evm,httpx]" eth-account httpx
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from typing import Any

import httpx

ENDPOINT = "https://inferrail-cost-gateway.onrender.com/read/v1/page"
BASE_MAINNET = "eip155:8453"


class PageNotBought(Exception):
    """The page wasn't delivered (and so wasn't charged): status and Web Read's error body."""

    def __init__(self, status: int, body: Any) -> None:
        super().__init__(f"HTTP {status}: {body}")
        self.status, self.body = status, body


async def buy_page(
    url: str,
    account: Any,
    *,
    endpoint: str = ENDPOINT,
    request_id: str | None = None,
    max_usd: str = "0.002",
    network: str = BASE_MAINNET,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """The delivered page (markdown, title, word count, content hash and a receipt)."""
    from x402 import x402Client
    from x402.http.clients.httpx import PaymentError, x402AsyncTransport
    from x402.mechanisms.evm.exact import ExactEvmScheme

    payer = x402Client()
    payer.register(network, ExactEvmScheme(signer=account))
    payer.set_spend_controls({"max_amount_per_payment": f"${max_usd}"})
    body = {"url": url, "request_id": request_id or uuid.uuid4().hex}
    async with httpx.AsyncClient(
        transport=x402AsyncTransport(payer, transport=transport), timeout=60
    ) as http:
        for attempt in (1, 2):
            try:
                response = await http.post(endpoint, json=body)
                break
            except (httpx.TransportError, PaymentError) as error:
                # The SDK wraps a dropped connection on the paid request in PaymentError.
                lost = isinstance(error, httpx.TransportError) or isinstance(
                    error.__cause__, httpx.TransportError
                )
                if attempt == 2 or not lost:
                    raise
                # Same url and request_id from the same wallet: never charged twice.
    if response.status_code == 200:
        return dict(response.json())
    try:
        detail: Any = response.json()
    except ValueError:
        detail = response.text[:500]
    raise PageNotBought(response.status_code, detail)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("url")
    ap.add_argument("--request-id", help="reuse to retry a purchase without a second charge")
    ap.add_argument(
        "--max-usd", default="0.002", help="refuse any offer above this (default 0.002)"
    )
    ap.add_argument("--endpoint", default=ENDPOINT)
    args = ap.parse_args(argv)
    key = os.environ.get("EVM_PRIVATE_KEY")
    if not key:
        print("set EVM_PRIVATE_KEY to your agent's Base wallet key", file=sys.stderr)
        return 2
    from eth_account import Account

    try:
        page = asyncio.run(
            buy_page(
                args.url,
                Account.from_key(key),
                endpoint=args.endpoint,
                request_id=args.request_id,
                max_usd=args.max_usd,
            )  # fmt: skip
        )
    except PageNotBought as exc:
        print(json.dumps({"status": exc.status, "error": exc.body, "charged": False}))
        return 1
    print(json.dumps(page))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
