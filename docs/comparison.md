# How Inferrail compares

This page states only claims that can be verified against Inferrail's own
code/documentation, or against another project's own public documentation,
as of the date noted. It does not claim any other project is broken, unsafe,
or worse — only that a specific mechanism differs.

## Payload handling: schema-level vs. configurable

Most self-hosted AI gateways can be configured not to log prompts and
responses. Inferrail does not log them by a different mechanism: its
telemetry and receipt types have no field capable of holding prompt or
response text at all.

**Inferrail:** `InferenceEvent` and `InferenceReceipt`
(`src/inferrail/telemetry/events.py`, `src/inferrail/receipts/schema.py`)
record `prompt_tokens`/`completion_tokens` as integer counts. Neither type,
nor anything upstream of it, has a field that can hold the prompt or
response text — there is nothing to configure, toggle, or accidentally
leave on. This is a schema-level property, covered by a test, not a runtime
setting. See
[ADR-0003](adr/0003-no-payload-persistence-by-default.md) and
[ADR-0005](adr/0005-privacy-preserving-economic-receipts.md).

**LiteLLM** (a widely-used open-source AI gateway), by contrast, documents
`store_prompts_in_spend_logs` as a configurable setting — a boolean in
`general_settings` (config file, or toggleable from its UI without a
restart) that controls whether request/response content is stored in its
spend-logs table. See LiteLLM's own documentation:
<https://docs.litellm.ai/docs/proxy/ui_spend_log_settings> (checked
2026-09-10). This is a legitimate design choice for a product built around
full-fidelity logging by default — the point here is only that it is a
setting, not a structural guarantee.

Inferrail does not claim that excluding payloads from telemetry is a
unique *behavior* — many gateways can be configured to do the same thing.
The claim is narrower: Inferrail's version of that property is enforced by
the data model itself, not by a setting that has to stay correctly
configured.

## Self-hosted, no control-plane dependency

Inferrail's gateway has zero code path that calls out to, depends on, or
assumes an Inferrail-operated service exists — see
[ADR-0004](adr/0004-data-plane-control-plane-boundary.md). It runs entirely
on infrastructure you control. (Inferrail also offers two separate, optional
hosted capabilities — Work Economics and Economic Authority — but neither
is required to run the gateway, and the gateway has no dependency on them.)

## What this page does not claim

This page does not compare pricing, feature breadth, provider coverage,
reliability, or any other project's roadmap — those change often and
aren't things this repository can verify about another project. For
Inferrail's own current scope and explicit non-goals, see
[docs/PRODUCT.md](PRODUCT.md).
