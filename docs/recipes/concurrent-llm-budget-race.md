# Why per-run LLM budgets leak under concurrent calls

A per-run budget usually works like this: before each model call, read how
much the run has spent, compare it with the limit, then make the call and
record its cost when the response comes back. That is exact when calls run
one at a time. It isn't when they run at once.

## The race

Every call that starts before the first one finishes reads the same "spent
so far". Parallel tool calls, sub-agents working at the same time, a retry
racing the original, or a batch job with a thread pool can all pass the
check together. The cost of a call isn't known until its response ends, so
the window isn't a few milliseconds: it's the whole duration of the call.

With a budget worth 10 calls and 30 calls started together, a check-then-record
budget lets all 30 through.

## What holds

1. **Reserve before the provider, in the same step as the check.** Admit a
   call only if `limit - committed - outstanding reservations >= estimate`,
   and record the reservation in that same transaction. The estimate is the
   input tokens plus `max_tokens` at the model's price, which is why
   budgets need `max_tokens` set.
2. **Reconcile afterwards.** Replace the reservation with the actual cost
   when usage comes back. If a call costs more than its estimate, let it
   finish, record the overrun and refuse the run's later calls.
3. **Keep the reservation when you can't tell what happened.** A stream
   that is cancelled, times out or ends without usage may still have been
   billed. Counting it against the run is the safe choice.
4. **Make the refusal non-retryable.** If it looks like a 429 or a 5xx, SDK
   and framework retry logic will send it again.
5. **Key the budget by the unit you care about.** The run, job or ticket,
   not the API key or the process, and pass the same id on every call,
   including sub-agents.

## Test your own setup

The [per-run budget benchmark](https://github.com/domondi1/per-run-budget-benchmark)
is a small reproducible test: a fake OpenAI-compatible upstream, a budget
worth exactly 10 calls at true cost, then 30 calls at once (plus 5 more), 15
calls one at a time, and the same with streams. It counts how many calls
reached the upstream. Point `burst.py` at any OpenAI-compatible endpoint
that enforces a budget:

```bash
uvicorn fake_upstream:app --port 9400
python burst.py --url http://127.0.0.1:<port>/v1/chat/completions --nonce --n 30
```

It has already been useful outside this project: it showed that one proxy's
per-run cap let a 30-call burst through, the proxy's maintainer traced it to
check-then-record admission and fixed it by reserving at admission, and the
rerun now holds at 10. The results table lists the exact versions and
configs tested, including an earlier Inferrail release that had the same
problem.

## Doing it with Inferrail

Inferrail implements the steps above as an OpenAI- and Anthropic-compatible
gateway that runs in your process. The run's id and budget are two request
headers, the reservation happens in one SQLite transaction, and a refused
call gets HTTP 402 before it reaches the provider:

```python
import inferrail
from openai import OpenAI

base_url = inferrail.start()
client = OpenAI(base_url=base_url, api_key="unused", default_headers={
    "X-Inferrail-Attribute-Work-Id": "run-7f3a",
    "X-Inferrail-Budget-Usd": "0.50",
})
```

Framework-specific setup (LangGraph, CrewAI, the OpenAI Agents SDK, Strands,
ADK, DSPy, the Vercel AI SDK and more) is in
[Give one AI agent run a dollar budget](agent-run-budget.md).
