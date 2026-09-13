# Inferrail Founder Demo Script

**Duration:** ~7-8 minutes live (3 minutes scripted, 4-5 minutes live interaction)

---

## Opening (20-30 seconds)

> "If you run AP invoice processing with an LLM in the loop, you already have exceptions: a field extracted with low confidence, or a deterministic check that failed. Today, whoever built that pipeline has to decide, by hand, whether it's worth one more automated attempt or whether it goes straight to a human reviewer — and there's usually no record of what that decision actually cost versus what the alternative would have cost. Inferrail decides that one retry-vs-review call for you, executes it, and records the resulting cost and outcome so you can check whether the policy is actually saving money against your own historical data. That's what I'm going to show you."

---

## Setup (Assumed)

- Terminal open in a clean directory or cloned `inferrail` repo
- Python 3.11+
- (Optional but recommended: show this is on your laptop, not a hosted service)

### Commands to run, in order:

```bash
# Install (SDK + CLI; the fixture-based demo below needs no extra deps)
pip install inferrail

# Run the synthetic, fixture-based demo (zero API key, zero network, ~1 second)
inferrail ap demo
```

---

## Show #1: The AP Decision + Report (2-3 minutes)

**Say before running:**

> "This demo walks five scenarios against the exact same decision engine a real integration uses: an eligible exception that the one permitted retry actually recovers, one where the retry doesn't resolve it and falls back to your existing human-review path, one where the policy sends it straight to review without ever calling the retry adapter, a repeated request handled idempotently, and a recorded human-review outcome. Watch scenario 1:"

```bash
inferrail ap demo
```

**Expected output (excerpt):**
```
1. Eligible exception, one retry recovers it:
   recommended=retry status=retry_resolved retry_status=success
   recovered fields (usable data, not just a status): {'invoice_number': 'INV-10482', 'vendor': 'Acme Supply Co', 'total': '1420.00'}
```

**Pause here and highlight:**

- "That's not just a status — those are the actual re-extracted invoice fields coming back to the caller. A success signal alone isn't the point; usable data is."
- "The policy that made this call is a configurable heuristic on your own extraction confidence — we don't claim it's a calibrated economic optimum. What it does guarantee: at most one retry, a fixed fallback to your existing human-review path, never a fabricated outcome for an action that wasn't taken."

**Then show the full report:**

```bash
inferrail ap report --db inferrail-ap-demo.sqlite3
```

**Expected output:**
```
demo-retry-succeeds         decision=dec_... action=retry        status=retry_resolved        retry_status=success validation_passed=True  established_outcome=None
demo-retry-fails            decision=dec_... action=retry        status=awaiting_human_review retry_status=failed  validation_passed=False established_outcome=None
demo-policy-disallows-retry decision=dec_... action=human_review status=resolved              retry_status=-       validation_passed=None  established_outcome=corrected
```

**Point out:**

- "`demo-retry-fails` is still `awaiting_human_review` — that row's cost is honestly incomplete until a real review outcome comes back, never silently marked done."
- Run `inferrail ap report --db inferrail-ap-demo.sqlite3 --json` for the full auditable record per work_id, including `sunk_cost_usd`, `retry_cost_usd`, `pre_flight_estimate_usd`, `retry_cost_overrun_usd`, and `observed_cost_complete` — this is the same record a real integration inspects to check the policy against its own history.

---

## Show #2: One Full Decision Record (1 minute)

**Say:**

> "Let's open one full record to show exactly what's tracked, and what isn't:"

```bash
inferrail ap report --db inferrail-ap-demo.sqlite3 --json | python3 -m json.tool
```

**Point out on the `demo-retry-succeeds` row:**

- `checkpoint_attempt_id`, `retry_attempt_id`, and `decision_id` are all preserved — a decision can always be traced back to the extraction attempt that triggered it.
- `pre_flight_estimate_usd` is the adapter's own bound on the retry's cost, checked *before* the retry was authorized — separate from `sunk_cost_usd` (what was already spent before this decision).
- No invoice content or provider credentials appear anywhere in this record — see "Privacy Boundary," below.

---

## Show #3 (secondary): The Gateway/Work Economics Substrate (1 minute, optional)

**Say:**

> "AP builds on Inferrail's original product: a self-hosted, OpenAI-compatible gateway that turns supported chat-completion traffic into local, payload-free economic receipts. If that's more relevant to what you're building, here's the same idea one level down:"

```bash
inferrail demo
inferrail report --by customer
```

See the gateway's own receipt/report walkthrough in `README.md` for the full script — this remains a real, working, unchanged capability; it's just no longer the lead of this demo.

---

## Value Moment (Closing / ~30 seconds)

**Summarize:**

> "That's the core loop: one eligible exception, one permitted automated attempt or your existing review path, executed and recorded honestly enough that you can check whether the policy is actually worth it against your own data. No claim of proven savings here — the auditable report is exactly what you'd use to find out for your own volumes."

---

## Privacy Boundary (Technical Proof)

**If asked "Can Inferrail see my invoice content?"**

> "No. The retry adapter runs entirely in your own process — invoice text goes from your process directly to whichever extraction provider you configure, never through an Inferrail-operated service. If you use the optional hosted API instead of a local store, it receives only identifiers, policy-config numbers, confidence/validation results, cost figures, and status enums — never invoice field values or provider credentials. The schema has no field for them."

---

## Deployment Boundary (One Sentence)

**If asked "Is this production-ready?"**

> "v0.2.0's AP module is a first release: exactly one retry per case, two supported failure types, and a policy that's an honest heuristic, not calibrated optimization — see docs/capabilities/ap-invoice-exception-recovery.md's 'What this does not do' for the full bounded scope."

---

## Close: The Qualifying Question

> "Does your AP pipeline already produce these two kinds of exceptions — low-confidence extractions or failed deterministic checks — and do you already have a human-review path we'd be routing into, or would we be starting from scratch on the review side?"

**Listen for:**

- An existing review queue/ticketing system + real exception volume = **strong signal**
- No existing review path = Inferrail doesn't build one for you (see "What this does not do") — still useful, but a bigger lift
- Interest in the underlying cost-attribution substrate rather than AP specifically = point them at the gateway/Work Economics instead

---

## After the Demo (Optional Real Provider Test)

If the early adopter has an OpenAI key and wants to see the same engine against a real extraction call:

```bash
OPENAI_API_KEY=sk-... python examples/ap_invoice_exception_recovery/live_openai_demo.py
```

Makes exactly one real, billed OpenAI call against a synthetic (not customer) invoice, prints the pre-flight cost authorization decision before the call, and asserts the real re-extracted fields came back — not just that the call succeeded. Suitable for 1-on-1 technical evaluation, not a mass walkthrough.

---

## Artifacts to Preserve

After the demo, save:
- `inferrail-ap-demo.sqlite3` / `inferrail-ap-demo-handoffs.jsonl` — the raw store and handoff log, to show a skeptical person the actual persisted record
- Screenshot of the `--json` report output — use for follow-up email if the person is interested

---

## Notes for the Founder

- **Do not oversell beyond v0.2.0's bounded scope.** One retry per case, two failure types, a heuristic policy — stick to "today it does Y," see the capability doc's "What this does not do."
- **Emphasize the payload-free/data-boundary property by pointing at the record, not just claiming it.** Open the `--json` report. Show them there is no invoice field or credential anywhere in it.
- **Use `demo-retry-fails`'s incomplete cost as a teaching moment.** It's the clearest proof the report isn't just declaring victory — a case that hasn't actually resolved stays honestly marked incomplete.
- **If they ask about the hosted option** — a hosted decision/persistence/reporting API exists, but retry execution always happens in the customer's own process; point them at `hosted/ap_exceptions/README.md`.
