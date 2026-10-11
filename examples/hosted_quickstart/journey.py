"""The whole Inferrail Hosted customer journey, with the standard OpenAI and Anthropic SDKs.

Every step uses only what the service publishes. It prints what happened and the next action,
labelled [mock provider] or [live provider] so a stand-in is never mistaken for a real model.

    # Local: Inferrail Hosted on this machine, stand-ins for OpenAI and Anthropic. No keys, no cost.
    pip install -e ".[ap]" anthropic -r hosted/budget_gateway/requirements.txt
    python examples/hosted_quickstart/journey.py

    # Live: a Hosted deployment and your own provider key(s). A few tiny calls, capped by the
    # run ceilings below (well under $0.01 of your provider usage).
    python examples/hosted_quickstart/journey.py --base-url https://<host> \\
        --openai-key sk-... [--anthropic-key sk-ant-...]

Steps:
1. discover: GET /pricing (terms, limits, whether credits can be bought);
2. create a workspace (no account);
3. a budgeted run with the OpenAI SDK;
4. the same with the Anthropic SDK;
5. receipts for each run (GET /v1/work/<id>) and usage (GET /v1/workspace);
6. a call over its run ceiling, refused before the provider (402, not billed);
7. what to do when the free allowance is used up (from /pricing).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import socket
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx

HOSTED = Path(__file__).resolve().parents[2] / "hosted" / "budget_gateway"


def step(n: int, title: str) -> None:
    print(f"\n[{n}] {title}")


def journey(
    base_url: str, openai_key: str | None, anthropic_key: str | None, label: str
) -> dict[str, Any]:
    from anthropic import Anthropic
    from openai import APIStatusError, OpenAI

    out: dict[str, Any] = {"provider": label}
    http = httpx.Client(timeout=60)

    step(1, "Discover the terms")
    pricing = http.get(f"{base_url}/pricing").json()
    print(
        f"    {pricing['free_governed_runs_per_month']} free runs/month, then "
        f"${pricing['price_per_governed_run_usd']}/run; credit purchases open: "
        f"{pricing.get('credit_purchases_open', bool(pricing['credits']))}"
    )
    out["pricing_ok"] = "free_governed_runs_per_month" in pricing

    step(2, "Create a workspace (no account)")
    key = http.post(f"{base_url}/v1/workspaces").json()["api_key"]
    auth = {"Authorization": f"Bearer {key}"}
    print("    got a workspace key (shown once; Inferrail stores only its hash)")

    runs: dict[str, str] = {}
    if openai_key:
        step(3, f"A budgeted run with the OpenAI SDK [{label}]")
        oa = OpenAI(
            base_url=f"{base_url}/v1",
            api_key=key,
            default_headers={"X-Provider-Api-Key": openai_key},
            max_retries=0,
        )
        work = f"journey-openai-{uuid.uuid4().hex[:6]}"
        r = oa.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=8,
            messages=[{"role": "user", "content": "Reply with one word: ok"}],
            extra_headers={
                "X-Inferrail-Attribute-Work-Id": work,
                "X-Inferrail-Budget-Usd": "0.01",
            },
        )
        print(f"    answered: {r.choices[0].message.content!r}")
        runs["openai"] = work
    if anthropic_key:
        step(4, f"A budgeted run with the Anthropic SDK [{label}]")
        an = Anthropic(
            base_url=base_url,  # no /v1: the SDK adds it
            api_key=key,
            default_headers={"X-Provider-Api-Key": anthropic_key},
            max_retries=0,
        )
        work = f"journey-anthropic-{uuid.uuid4().hex[:6]}"
        m = an.messages.create(
            model="claude-haiku-4-5",
            max_tokens=8,
            messages=[{"role": "user", "content": "Reply with one word: ok"}],
            extra_headers={
                "X-Inferrail-Attribute-Work-Id": work,
                "X-Inferrail-Budget-Usd": "0.01",
            },
        )
        print(f"    answered: {m.content[0].text!r}")
        runs["anthropic"] = work

    step(5, "Receipts and usage")
    out["receipts"] = {}
    for provider, work in runs.items():
        w = http.get(f"{base_url}/v1/work/{work}", headers=auth).json()
        print(f"    {provider}: {w['calls']} call(s), ${w['cost_usd']} for run {work!r}")
        out["receipts"][provider] = {"calls": w["calls"], "cost_usd": w["cost_usd"]}
    usage = http.get(f"{base_url}/v1/workspace", headers=auth).json()
    print(
        f"    workspace: {usage['governed_runs']} governed run(s) this month, "
        f"{usage['free_runs_remaining']} free left, {usage['credits_remaining']} credits"
    )
    out["governed_runs"] = usage["governed_runs"]

    step(6, "A call over its run ceiling")
    first = next(iter(runs.values()), None)
    if openai_key:
        try:
            oa.chat.completions.create(
                model="gpt-4o-mini",
                max_tokens=500,
                messages=[{"role": "user", "content": "Write a long essay."}],
                extra_headers={
                    "X-Inferrail-Attribute-Work-Id": f"journey-tiny-{uuid.uuid4().hex[:6]}",
                    "X-Inferrail-Budget-Usd": "0.00001",
                },
            )
            out["refused"] = False
        except APIStatusError as e:
            err = e.body.get("error", e.body) if isinstance(e.body, dict) else {}
            print(f"    HTTP {e.status_code} {err.get('type')}: refused before the provider")
            print(f"    next action: {err.get('remediation')}")
            out["refused"] = e.status_code == 402
        after = http.get(f"{base_url}/v1/workspace", headers=auth).json()
        out["refusal_billed"] = after["governed_runs"] != usage["governed_runs"]
        print(f"    billed for the refused run: {'yes' if out['refusal_billed'] else 'no'}")

    step(7, "When the free allowance is used up")
    rails = pricing["credits"]
    if rails:
        for name, rail in rails.items():
            print(f"    buy credits via {name}: {rail.get('url', rail['endpoint'])}")
    else:
        print("    calls get HTTP 402 allowance_exhausted, with the reset time; credit purchases")
        print("    are not open on this deployment yet")
    print(f"    full contract for agents: {base_url}/llms.txt")
    out["first_run"] = first
    http.close()
    return out


# -- local mode ---------------------------------------------------------------------------------
def _load(name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, HOSTED / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _stand_in(anthropic: bool) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        n = body.get("max_tokens", 8)
        if anthropic:
            return httpx.Response(
                200,
                json={
                    "id": "msg_local",
                    "type": "message",
                    "role": "assistant",
                    "model": body["model"],
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 12, "output_tokens": n},
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-local",
                "object": "chat.completion",
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": n, "total_tokens": 12 + n},
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def run_local() -> dict[str, Any]:
    import uvicorn

    with tempfile.TemporaryDirectory() as tmp, socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        for name in ("workspaces", "stripe_checkout", "chain_check", "report"):
            if (HOSTED / f"{name}.py").exists():
                _load(name, f"{name}.py")
        service = _load("budget_gateway_service", "service.py")
        service.openai_client_factory = lambda: _stand_in(False)
        service.anthropic_client_factory = lambda: _stand_in(True)
        base = f"http://127.0.0.1:{port}"
        app = service.create_app(
            service.Settings.from_env({"BG_DATA_DIR": tmp, "BG_PUBLIC_BASE_URL": base})
        )
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        while not server.started:
            time.sleep(0.05)
        print("Inferrail Hosted is running locally with stand-ins for OpenAI and Anthropic.")
        try:
            return journey(base, "sk-local-stand-in", "sk-ant-local-stand-in", "mock provider")
        finally:
            server.should_exit = True
            thread.join(timeout=20)
            app.state.release_instance_lock()


def main(argv: list[str] | None = None) -> dict[str, Any]:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base-url", help="an Inferrail Hosted deployment, e.g. https://<host>")
    ap.add_argument("--openai-key", help="your OpenAI key (sent per request, never stored)")
    ap.add_argument("--anthropic-key", help="your Anthropic key (sent per request, never stored)")
    args = ap.parse_args(argv)
    if not args.base_url:
        return run_local()
    if not (args.openai_key or args.anthropic_key):
        ap.error("--base-url needs --openai-key and/or --anthropic-key")
    return journey(args.base_url.rstrip("/"), args.openai_key, args.anthropic_key, "live provider")


if __name__ == "__main__":
    main()
