# How Inferrail handles AI request data

Inferrail records the cost and context of an AI request without storing
the prompt or response. That choice is built into the receipt schema
rather than controlled by a logging switch.

This page explains that design and compares it with one other gateway
whose prompt-storage behavior can be changed through configuration. The
comparison is about how the systems work, not about judging the companies
behind them.

## The design

`InferenceEvent` and `InferenceReceipt`
(`src/inferrail/telemetry/events.py`, `src/inferrail/receipts/schema.py`)
record `prompt_tokens` and `completion_tokens` as counts, not text.
Nothing in those types, or anything upstream of them, has a field that
can hold the actual prompt or response. It isn't a setting that could get
left on by mistake. It's simply not part of what the data looks like. See
[ADR-0003](adr/0003-no-payload-persistence-by-default.md) and
[ADR-0005](adr/0005-privacy-preserving-economic-receipts.md) for the
reasoning behind it.

## How LiteLLM handles the same question

LiteLLM, a widely used open-source AI gateway, documents
`store_prompts_in_spend_logs` as something you can turn on or off, either
in its config file or from its UI without a restart. When it's on,
request and response content gets stored in its spend-logs table. See
LiteLLM's own docs: <https://docs.litellm.ai/docs/proxy/ui_spend_log_settings>
(checked 2026-09-10). That's a reasonable choice for a tool built around
full logging by default. The difference worth pointing out is just that
it's a setting someone has to get right and keep that way, while
Inferrail's approach doesn't depend on a setting at all.

## Running it yourself

Inferrail's gateway doesn't call out to, or depend on, any
Inferrail-operated service. See
[ADR-0004](adr/0004-data-plane-control-plane-boundary.md). You run it
entirely on your own infrastructure. Inferrail also offers two optional
hosted capabilities, Work Economics and Economic Authority, but the
gateway itself doesn't need either one.

## What this page isn't

Not a pricing comparison, not a feature list, not a claim about which
tool is better overall. Those things change often, and this repo has no
way to verify another project's roadmap. For what Inferrail itself does
and doesn't do today, see [docs/PRODUCT.md](PRODUCT.md).
