# AP Invoice-Exception Recovery — Examples

For one eligible invoice-extraction exception: decide whether it gets
one permitted machine retry or your established human-review path,
execute the retry, and record the resulting cost and outcome. Full
contract: [`docs/capabilities/ap-invoice-exception-recovery.md`](../../docs/capabilities/ap-invoice-exception-recovery.md).

## 1. Fixture-based, zero-key walkthrough (start here)

```bash
inferrail ap demo
```

Runs the real `RecoveryEngine` code path against canned, deterministic
results — no API key, no network access, no money spent. Covers all five
core scenarios: an eligible exception recovered by one retry, an
unsuccessful retry that falls back to human review, a case the policy
routes straight to human review, a repeated request handled idempotently
(never re-executed), and inspecting the resulting decision/outcome
records. Inspect the results afterward:

```bash
inferrail ap report --db inferrail-ap-demo.sqlite3
sqlite3 inferrail-ap-demo.sqlite3 "select * from decisions;"
cat inferrail-ap-demo-handoffs.jsonl
```

## 2. Real-provider execution (`live_openai_demo.py`)

**Live-provider execution — makes one real, billed OpenAI call.**

```bash
pip install "inferrail[ap]"
OPENAI_API_KEY=sk-... python examples/ap_invoice_exception_recovery/live_openai_demo.py
```

Runs the same engine against `OpenAIRetryAdapter`, which sends the
(synthetic, example-only) invoice text in this script directly from your
process to OpenAI — never through any Inferrail-operated service. See
"Data boundary" below.

## 3. Integrating your own extraction pipeline and review queue

```bash
python examples/ap_invoice_exception_recovery/custom_integration_example.py
```

Shows the two things you actually implement to integrate for real: a
`RetryAdapter` that calls your own extraction system (including
`estimate_cost`, required for a retry to be authorized at all — see
"Prospective retry-cost authorization" in the capability doc), and a
`HumanReviewHandoff` that enqueues into your own review queue/ticketing
system. Threads the full chain in one run: decision → retry → validation
→ usable recovered fields (not just a status) **or** an acknowledged
handoff → recorded outcome → the joined report. Everything else (policy,
persistence, idempotency) is provided.

## 4. The hosted API, from a client that never sends it invoice data

```bash
cd hosted/ap_exceptions && pip install -r requirements.txt && pip install -e ../..
AP_API_KEYS=dev-key-1 python3 service.py /tmp/inferrail_ap_hosted_example 8422 &

python examples/ap_invoice_exception_recovery/hosted_client_example.py
```

One coherent flow against the hosted decision/persistence/reporting API:
decision → local cost authorization and retry execution (never inside the
hosted service) → recorded attempt → an example review receiver on
validation failure → recorded resolution → retrieved report. Also works
against a real deployed instance via `--base-url`/`--api-key`.

## Data boundary

Invoice contents and provider credentials stay in your own process. The
SDK never sends either to any Inferrail-operated service — only
identifiers, the policy config, confidence/validation numbers, cost
figures, and status enums are ever recorded in the local `RecoveryStore`
(or a hosted Inferrail AP service, if you use one). `OpenAIRetryAdapter`
sends invoice text directly from your process to OpenAI, exactly like a
real extraction pipeline you already run would.
