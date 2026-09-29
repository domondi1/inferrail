"""Give one AI agent run a dollar budget.

One run fans out into several model calls at once. Every call carries the
same run id and the run's budget; Inferrail admits calls while the budget
has room and refuses the rest with HTTP 402 *before* they reach the
provider. See docs/recipes/agent-run-budget.md.

    inferrail serve --config inferrail.yaml     # budgets enabled, see the recipe
    python examples/agent_run_budget.py         # then: inferrail work <run id>

Environment:
    INFERRAIL_URL     gateway base URL (default http://127.0.0.1:8000/v1)
    RUN_BUDGET_USD    the run's budget in USD (default 0.05)
    PARALLEL_CALLS    calls sent at once (default 8)
"""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass

import httpx
from openai import APIStatusError, AsyncOpenAI


@dataclass
class RunResult:
    run_id: str
    answered: int
    refused_by_budget: int


async def run_agent_step(
    *,
    base_url: str,
    budget_usd: str,
    parallel_calls: int,
    model: str = "gpt-4o-mini",
    http_client: httpx.AsyncClient | None = None,
) -> RunResult:
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    client = AsyncOpenAI(
        base_url=base_url,
        api_key=os.environ.get("INFERRAIL_GATEWAY_TOKEN", "unused"),
        max_retries=0,
        http_client=http_client,
        default_headers={
            # 1. Mark the logical work: every call of this run shares the id.
            "X-Inferrail-Attribute-Work-Id": run_id,
            # 2. Declare the run's dollar budget. No budget object to create.
            "X-Inferrail-Budget-Usd": budget_usd,
        },
    )

    async def one_call(i: int) -> str:
        try:
            await client.chat.completions.create(
                model=model,
                max_tokens=200,  # keeps each call's reservation close to its real cost
                messages=[{"role": "user", "content": f"Summarize item {i} in one line."}],
            )
            return "answered"
        except APIStatusError as exc:
            if exc.status_code == 402:  # refused before the provider: budget used up
                return "refused"
            raise

    # 3. The run fans out: all calls at once, all against one budget.
    outcomes = await asyncio.gather(*(one_call(i) for i in range(parallel_calls)))
    return RunResult(run_id, outcomes.count("answered"), outcomes.count("refused"))


def main() -> None:
    result = asyncio.run(
        run_agent_step(
            base_url=os.environ.get("INFERRAIL_URL", "http://127.0.0.1:8000/v1"),
            budget_usd=os.environ.get("RUN_BUDGET_USD", "0.05"),
            parallel_calls=int(os.environ.get("PARALLEL_CALLS", "8")),
        )
    )
    print(f"run {result.run_id}: {result.answered} answered, "
          f"{result.refused_by_budget} refused by the run's budget")
    # 4. The run's final cost: inferrail work <run id> --config inferrail.yaml
    print(f"final run cost: inferrail work {result.run_id} --config inferrail.yaml")


if __name__ == "__main__":
    main()
