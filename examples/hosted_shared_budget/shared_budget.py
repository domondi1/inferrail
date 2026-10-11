"""Many agents, one run budget: what Inferrail Hosted adds to the free self-hosted gateway.

Several independent processes (think: agents on different machines) use the standard OpenAI SDK
against one Inferrail Hosted workspace. They all work on the same run (one work id) with one
dollar ceiling, and they all hammer it concurrently. The ceiling holds: their combined spend
never crosses it, and every call that would have crossed it is refused before reaching the
provider. Then the service restarts, and the run's spend and the refusals are still there.

Two ways to run it:

    # Local, no keys, no cost. Starts Inferrail Hosted on this machine with a stand-in for
    # OpenAI that answers instantly and reports token usage like the real API.
    pip install -e ".[ap]" -r hosted/budget_gateway/requirements.txt
    python examples/hosted_shared_budget/shared_budget.py

    # Against a real Inferrail Hosted deployment with your own OpenAI key (costs a few cents
    # of your OpenAI usage; the run ceiling below caps it).
    python examples/hosted_shared_budget/shared_budget.py \
        --base-url https://<host> --provider-key sk-... --budget 0.02

Options: --agents N (default 6), --calls-per-agent N (default 25), --budget USD (default 0.005).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import multiprocessing as mp
import socket
import sys
import tempfile
import threading
import time
import uuid
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
HOSTED = ROOT / "hosted" / "budget_gateway"
MODEL = "gpt-4o-mini"
MAX_TOKENS = 200


# -- the agents: plain OpenAI SDK, one extra header each ------------------------------------------
def agent(
    base_url: str,
    workspace_key: str,
    provider_key: str,
    work_id: str,
    budget: str,
    calls: int,
    name: str,
    out: Any,
) -> None:
    """One independent agent process. Nothing Inferrail-specific beyond the base URL, the
    workspace key and three headers."""
    from openai import APIStatusError, OpenAI

    client = OpenAI(
        base_url=f"{base_url}/v1",
        api_key=workspace_key,
        default_headers={"X-Provider-Api-Key": provider_key},
        max_retries=0,
    )
    answered = refused = other = 0
    for i in range(calls):
        try:
            client.chat.completions.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                messages=[{"role": "user", "content": f"{name} step {i}: one word, please."}],
                extra_headers={
                    "X-Inferrail-Attribute-Work-Id": work_id,
                    "X-Inferrail-Budget-Usd": budget,
                },
            )
            answered += 1
        except APIStatusError as e:
            if e.status_code == 402:
                refused += 1
            else:
                other += 1
    out.put({"agent": name, "answered": answered, "refused": refused, "other_errors": other})


def run_agents(
    base_url: str, key: str, provider_key: str, work_id: str, budget: str, n: int, calls: int
) -> list[dict[str, int]]:
    ctx = mp.get_context("spawn")  # truly separate interpreters, like separate machines
    out = ctx.Queue()
    procs = [
        ctx.Process(
            target=agent,
            args=(base_url, key, provider_key, work_id, budget, calls, f"agent-{i + 1}", out),
        )
        for i in range(n)
    ]
    for p in procs:
        p.start()
    results = [out.get(timeout=300) for _ in procs]
    for p in procs:
        p.join(timeout=30)
    return sorted(results, key=lambda r: r["agent"])


# -- local mode: Inferrail Hosted on this machine, with a stand-in provider -----------------------
def _load(name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, HOSTED / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _stand_in_openai() -> httpx.AsyncClient:
    """Answers like OpenAI's chat completions API, reporting the full max_tokens as used (the
    most expensive honest answer). Nothing leaves this machine."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                "object": "chat.completion",
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": body.get("max_tokens", 16),
                    "total_tokens": 20 + body.get("max_tokens", 16),
                },
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class LocalHosted:
    """Inferrail Hosted in this process, on a data directory that survives `restart()`."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._app: Any = None

    def start(self) -> None:
        import uvicorn

        _load("workspaces", "workspaces.py")
        _load("stripe_checkout", "stripe_checkout.py")
        _load("chain_check", "chain_check.py")
        service = _load("budget_gateway_service", "service.py")
        service.openai_client_factory = _stand_in_openai  # the only substitution
        settings = service.Settings.from_env(
            {"BG_DATA_DIR": str(self.data_dir), "BG_PUBLIC_BASE_URL": self.base_url}
        )
        self._app = service.create_app(settings)
        config = uvicorn.Config(self._app, host="127.0.0.1", port=self.port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                if httpx.get(f"{self.base_url}/health", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.1)
        raise RuntimeError("local Inferrail Hosted did not start")

    def stop(self) -> None:
        self._server.should_exit = True
        assert self._thread is not None
        self._thread.join(timeout=20)
        self._app.state.release_instance_lock()

    def restart(self) -> None:
        self.stop()
        self.start()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# -- the demonstration ----------------------------------------------------------------------------
def work_record(base_url: str, key: str, work_id: str) -> dict[str, Any]:
    r = httpx.get(
        f"{base_url}/v1/work/{work_id}", headers={"Authorization": f"Bearer {key}"}, timeout=30
    )
    r.raise_for_status()
    return dict(r.json())


def demonstrate(
    base_url: str,
    provider_key: str,
    agents: int,
    calls: int,
    budget: str,
    hosted: LocalHosted | None,
) -> dict[str, Any]:
    key = httpx.post(f"{base_url}/v1/workspaces", timeout=30).json()["api_key"]
    work_id = f"shared-run-{uuid.uuid4().hex[:8]}"
    print(
        f"Workspace created. Run {work_id!r}, ceiling ${budget}, "
        f"{agents} agents x {calls} calls each, all at once.\n"
    )

    t0 = time.time()
    results = run_agents(base_url, key, provider_key, work_id, budget, agents, calls)
    elapsed = time.time() - t0
    for r in results:
        print(
            f"  {r['agent']:>9}: {r['answered']:3d} answered, {r['refused']:3d} refused (402)"
            + (f", {r['other_errors']} other errors" if r["other_errors"] else "")
        )
    answered = sum(r["answered"] for r in results)
    refused = sum(r["refused"] for r in results)
    record = work_record(base_url, key, work_id)
    spent = Decimal(record["cost_usd"])
    print(
        f"\n  {answered} calls answered, {refused} refused before the provider, in {elapsed:.1f}s."
    )
    print(
        f"  Run spend recorded by Inferrail: ${spent} of ${budget} "
        f"({'within' if spent <= Decimal(budget) else 'OVER'} the ceiling)."
    )

    summary: dict[str, Any] = {
        "work_id": work_id,
        "budget_usd": budget,
        "answered": answered,
        "refused": refused,
        "other_errors": sum(r["other_errors"] for r in results),
        "spent_usd": str(spent),
        "within_budget": spent <= Decimal(budget),
        "receipts": record["calls"],
    }
    if hosted is not None:
        hosted.restart()
        after = work_record(base_url, key, work_id)
        late = run_agents(base_url, key, provider_key, work_id, budget, 1, 1)[0]
        print(
            f"\n  Restarted the service on the same data directory: the run still shows "
            f"${after['cost_usd']} spent over {after['calls']} receipts, and a new call is "
            f"{'refused' if late['refused'] else 'ANSWERED'}."
        )
        summary["after_restart"] = {
            "spent_usd": after["cost_usd"],
            "receipts": after["calls"],
            "new_call_refused": late["refused"] == 1,
        }
    return summary


def main(argv: list[str] | None = None) -> dict[str, Any]:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base-url", help="an Inferrail Hosted deployment, e.g. https://<host>")
    ap.add_argument("--provider-key", help="your OpenAI key (with --base-url)")
    ap.add_argument("--agents", type=int, default=6)
    ap.add_argument("--calls-per-agent", type=int, default=25)
    ap.add_argument("--budget", default="0.005")
    args = ap.parse_args(argv)
    if args.base_url:
        if not args.provider_key:
            ap.error("--base-url needs --provider-key (your OpenAI key; never stored)")
        return demonstrate(
            args.base_url.rstrip("/"),
            args.provider_key,
            args.agents,
            args.calls_per_agent,
            args.budget,
            None,
        )
    with tempfile.TemporaryDirectory() as tmp:
        hosted = LocalHosted(Path(tmp) / "hosted-data")
        hosted.start()
        print("Inferrail Hosted is running locally with a stand-in for OpenAI (no key, no cost).")
        try:
            return demonstrate(
                hosted.base_url,
                "sk-local-stand-in",
                args.agents,
                args.calls_per_agent,
                args.budget,
                hosted,
            )
        finally:
            hosted.stop()


if __name__ == "__main__":
    main()
