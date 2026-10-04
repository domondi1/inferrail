"""An agent process. It holds an authority token and nothing else.

No wallet, no private key, no provider key: it can only ask the authority
runtime to spend on its behalf, and the runtime decides. Run by demo.py
as a separate process with a stripped environment.
"""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

AUTHORITY = os.environ["AUTHORITY_URL"]
TOKEN = os.environ["AUTHORITY_TOKEN"]
SELLER = os.environ["SELLER_URL"]


def call(path: str, body: dict[str, Any], token: str = TOKEN) -> dict[str, Any]:
    r = httpx.post(
        f"{AUTHORITY}{path}", json=body, headers={"Authorization": f"Bearer {token}"}, timeout=60
    )
    return r.json() | {"http_status": r.status_code}


def reply(r: dict[str, Any]) -> str:
    return str(r["body"]["choices"][0]["message"]["content"])


def say(role: str, text: str) -> None:
    print(f"  [{role}] {text}", flush=True)


def parent() -> None:
    r = call(
        "/model",
        {
            "messages": [{"role": "user", "content": "Plan research on agent budgets."}],
            "max_tokens": 300,
        },
    )
    say("parent", f"model call                     -> cost ${r['cost_usd']}")
    r = call(
        "/pay", {"method": "GET", "url": f"{SELLER}/search", "params": {"q": "budgets delegation"}}
    )
    say(
        "parent",
        f"x402 paid search    $0.002     -> {r['state']}, top hit: {r['body']['results'][0]['id']}",
    )
    r = call("/delegate", {"agent_id": "summarizer", "max_usd": "0.006"})
    say("parent", "delegated $0.006 to sub-agent 'summarizer' (token only, no key)")
    child_env = {
        "PATH": os.environ["PATH"],
        "AUTHORITY_URL": AUTHORITY,
        "AUTHORITY_TOKEN": r["token"],
        "SELLER_URL": SELLER,
        "ROLE": "child",
    }
    subprocess.run([sys.executable, __file__], env=child_env, check=True)

    say("parent", "8 concurrent x402 searches against what's left of the budget:")

    def one(_: int) -> str:
        return call(
            "/pay", {"method": "GET", "url": f"{SELLER}/search", "params": {"q": "x402"}}
        ).get("state", "REFUSED")

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(one, range(8)))
    say(
        "parent",
        f"  settled={outcomes.count('SETTLED')} refused-before-signing={outcomes.count('REFUSED')}",
    )


def child() -> None:
    r = call(
        "/pay",
        {
            "method": "POST",
            "url": f"{SELLER}/v1/chat/completions",
            "json": {
                "messages": [
                    {"role": "user", "content": "Summarize: x402 lets agents pay per request."}
                ]
            },
        },
    )
    say(
        "child ",
        f'x402 paid LLM call  $0.003     -> {r["state"]}: "{reply(r)}"',
    )
    r = call(
        "/pay", {"method": "GET", "url": f"{SELLER}/search", "params": {"q": "reconciliation"}}
    )
    say("child ", f"x402 paid search    $0.002     -> {r['state']}")
    r = call("/pay", {"method": "GET", "url": f"{SELLER}/search", "params": {"q": "eip-3009"}})
    say(
        "child ",
        f"x402 paid search    $0.002     -> REFUSED before signing "
        f"(remaining ${float(r['detail']['remaining_usd']):.3f})",
    )
    call("/finish", {})
    say("child ", "finished; unspent authority returns to the parent")


if __name__ == "__main__":
    leaked = [
        k
        for k in os.environ
        if any(s in k.upper() for s in ("KEY", "SECRET", "PRIVATE", "MNEMONIC"))
    ]
    if leaked:
        sys.exit(f"refusing to run: agent environment contains {leaked}")
    (child if os.environ.get("ROLE") == "child" else parent)()
