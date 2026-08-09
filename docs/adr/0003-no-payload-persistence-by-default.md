# 0003. No prompt/response payload persistence by default

## Status

Accepted

## Context

Inference telemetry is central to Inferrail's value, but prompts and
model responses are frequently sensitive (PII, proprietary business data,
regulated content). Infrastructure that silently persists or transmits
that content — even "just for debugging" — is both a trust liability and,
for a project whose stated strategy explicitly rules out surveillance and
dark patterns, a non-starter.

## Decision

`InferenceEvent` (`telemetry/events.py`) is schema-limited to operational
metadata: request id, timestamp, route, provider, model, status, latency,
token counts, retry count, error category. There is no field for message
content, prompt text, or response text anywhere in the schema — this is a
structural guarantee, not a runtime flag that could be misconfigured on,
and it's covered by a test (`test_inference_event_has_no_payload_fields`)
that fails if such a field is ever added without a conscious decision to
do so.

All v0.1 telemetry sinks (`ConsoleTelemetrySink`, `JSONLTelemetrySink`) are
local-only; none transmits data off the machine Inferrail runs on.

## Consequences

- Debugging a specific bad response by reading telemetry alone is not
  possible in v0.1 — this is the accepted cost of the privacy default.
- If payload capture is introduced later (e.g. for local debugging), it
  must be an explicit opt-in, almost certainly a separate mechanism from
  `InferenceEvent` rather than an extension of it, so the "telemetry never
  contains payloads" guarantee keeps holding for anyone who hasn't opted
  in.
- Any future Inferrail Cloud telemetry sink must be opt-in, documented,
  and under the developer's control — never a default. It will need to
  clear a materially higher trust bar than local sinks, precisely because
  it leaves the machine.
