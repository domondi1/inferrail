# Inferrail Founder Demo Script

**Duration:** ~7-8 minutes live (3 minutes scripted, 4-5 minutes live interaction)

---

## Opening (20-30 seconds)

> "Most teams running LLM applications get a bill from OpenAI at the end of the month. What they *don't* get is which customer, which task, or which feature actually spent that money. If you're running agents — multi-step workflows where one business task triggers multiple model calls — you have no way to know which task cost you what. That's the problem Inferrail solves right now."

---

## Setup (Assumed)

- Terminal open in a clean directory or cloned `inferrail` repo
- Python 3.11+
- (Optional but recommended: show this is on your laptop, not a hosted service)

### Commands to run, in order:

```bash
# Install from PyPI (public, v0.1.2)
pip install inferrail

# Run the synthetic demo (zero API key, zero network, takes ~2 seconds)
inferrail demo

# (Demo creates inferrail-demo-receipts.jsonl with 6 sample receipts)
```

---

## Show #1: The Receipt Report (1-2 minutes)

**Say before running:**

> "This demo simulates six LLM calls — three to handle one customer's request, two for another, and one unattributed. Each call goes through Inferrail, which records an economic receipt. Let's see what the aggregated cost looks like:"

```bash
# Show receipts grouped by customer
inferrail report --by customer --receipts inferrail-demo-receipts.jsonl
```

**Expected output:**
```
CUSTOMER        REQUESTS  FAILED  INPUT TOKENS  OUTPUT TOKENS  COST (USD)  UNKNOWN COST
globex          2                 1621          416            $0.007825
acme            3                 1952          361            $0.000483   1
(unattributed)  1                 300           50             $0.0001
--------------  --------  ------  ------------  -------------  ----------  -----------
TOTAL           6                 3873          827            $0.008408   1
```

**Pause here and highlight:**

- "Two calls for globex, three for acme. Costs are rolled up per customer without storing their prompts or responses."
- "That `1` in `UNKNOWN COST` — one request used a model we don't have pricing for. It shows `null`, not `$0`. We don't guess costs."

---

## Show #2: One Full Receipt (1 minute)

**Say:**

> "Let's open one actual receipt to prove we don't persist prompts:"

```bash
python -c "
import json
with open('inferrail-demo-receipts.jsonl') as f:
    receipt = json.loads(f.readline())
    print(json.dumps(receipt, indent=2))
" | head -30
```

**Expected output (showing payload-free schema):**
```json
{
  "receipt_id": "ir_...",
  "request_id": "req_...",
  "timestamp": "2026-08-22T...",
  "route": "default",
  "provider": "demo",
  "model": "demo-small",
  "status": "success",
  "prompt_tokens": 812,
  "completion_tokens": 143,
  "pricing": {
    "input_usd_per_million": "0.20",
    "output_usd_per_million": "0.80",
    "source": "DEMO — a made-up round number...",
    "verified_date": "2026-08-22"
  },
  "estimated_cost_usd": "0.000277",
  "attributes": {
    "customer": "acme",
    "workflow": "contract-review"
  },
  "total_latency_ms": 0.044,
  "retry_count": 0
}
```

**Point out:**

- "No `messages`, no `response`, no `choices`. Just economics: tokens, cost, who it was for, and timing."
- "The `attributes` object lets you tag calls with whatever business context matters: customer, workflow, feature, endpoint, AI agent name."

---

## Show #3: Multi-Call Task Aggregation (1-2 minutes)

**Say:**

> "Those six receipts came from two different business tasks. Watch what happens when we ask Inferrail to aggregate all calls sharing one task ID:"

```bash
# First, show what task IDs are in the receipts
grep '"task_id"' inferrail-demo-receipts.jsonl | head -3

# Then aggregate one task (task IDs are deterministic from the demo)
inferrail transaction "acme_task_001" --receipts inferrail-demo-receipts.jsonl
```

**What the user should see:**

- Multiple receipts roll up into a single `InferenceTransaction` object showing:
  - Total cost for that task
  - Total tokens (input + completion)
  - All sub-receipts that made up the task
  - Task succeeded or failed overall

---

## Value Moment (Closing / ~30 seconds)

**Summarize:**

> "That's the core insight: you can now tell an early-adopter or customer exactly what their specific request cost you in model spend — at the task level, not just per API call. You never store their prompts. And if a model's price isn't in our catalog, we say `null` instead of guessing. That's a foundation for cost-aware features and governance."

---

## Privacy Boundary (Technical Proof)

**If asked "Can Inferrail see my prompts?"**

> "Inferrail operates inline, so requests pass through it. But the receipt schema has no fields for prompts or responses — not configurable, not a flag, just absent from the Pydantic model. The tests enforce it. What Inferrail *does* store and persist: token counts, model, provider, cost, customer/task attribution, timing, and outcome."

---

## Deployment Boundary (One Sentence)

**If asked "Is this production-ready?"**

> "v0.1.2 is single-node only: one Inferrail process per receipts file. Multi-host aggregation, budgets, hosted control planes, and broad provider support (Anthropic, Bedrock, etc.) are not yet in scope — see the README's 'Supported today' and 'Not yet' sections."

---

## Close: The Qualifying Question

> "Which of these sounds closest to your current pain point: (A) you want to know what each customer costs you but are nervous about storing prompts, (B) you want to understand which features or workflows are most expensive, or (C) something else?"

**Listen for:**

- Anything involving multi-step/multi-call workflows + cost attribution + privacy concern = **strong signal**
- Single-call, not multi-call workflows = less immediate fit for v0.1 (but note it for future enhancements)
- Needs multi-host aggregation or enforcement = out of scope for v0.1, good to understand for roadmap

---

## After the Demo (Optional Real Provider Test)

If the early adopter has an OpenAI key and wants to see Inferrail against real data:

```bash
# Edit inferrail.yaml to point to real OpenAI
inferrail serve &
# Point your OpenAI client at http://localhost:8000 instead of api.openai.com
# Make a real call
# Check the receipt:
inferrail report --by customer
```

(This requires an `inferrail.yaml` and env var setup — suitable for 1-on-1 technical evaluation, not a mass walkthrough.)

---

## Artifacts to Preserve

After the demo, save:
- `inferrail-demo-receipts.jsonl` — show the skeptical person the raw JSONL to prove schema
- Screenshot of the report output — use for follow-up email if the person is interested

---

## Notes for the Founder

- **Do not oversell beyond v0.1.2 scope.** It's tempting to say "Inferrail will do X" when the ADR mentions it as a future direction. Stick to "today it does Y."
- **Emphasize the payload-free property by pointing at the schema, not just claiming it.** Open the JSON. Show them there is no `messages`, `response`, or `prompt` field.
- **Use the unknown pricing `1` in the report as a teaching moment.** It's the clearest proof that you're *not* guessing costs — you're explicitly saying "I don't know."
- **If they ask about agents ("Can an AI agent use this?")** — yes, Inferrail is B2A ready: it's self-contained, has MCP tools for spend/health queries, and agents can point their OpenAI client at it via `base_url`. But emphasize: agent adoption is a hypothesis, not shipped yet.

