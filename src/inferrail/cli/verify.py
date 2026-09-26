"""`inferrail verify-payload-free`: a schema inspection of the receipt
record Inferrail writes to disk, with its scope stated in the output.

What it checks: every field of the real, running `InferenceReceipt`
model (`inferrail.receipts.schema`), introspected at call time rather
than hardcoded, compared against a small set of payload field names
(`prompt`, `messages`, `content`, `response`). The same name check is a
regression test (`test_inference_receipt_has_no_payload_fields`,
`tests/unit/test_receipts.py`).

What it does not check: the receipt still has string fields and an
arbitrary `attributes: dict[str, str]` that is persisted exactly as the
caller supplies it, so a field-name check cannot prove that stored
strings are free of sensitive text, and it says nothing about logs,
telemetry sinks, or the upstream provider. The behavioral evidence that
the gateway does not copy message bodies into receipts is the canary
tests in `tests/unit/test_gateway_receipts.py`,
`tests/unit/test_gateway.py`, and `tests/unit/test_gateway_anthropic.py`.
The output says this plainly so it is not mistaken for a full audit. See
docs/adr/0020-quickstart-both-sdks-and-payload-free-verification.md.
"""

from __future__ import annotations

from inferrail.receipts.schema import InferenceReceipt

# The same disjoint-set check test_inference_receipt_has_no_payload_fields
# makes — kept here too (not imported from the test module, which pytest
# owns) so this command's check doesn't depend on the test suite being
# present in an installed, non-dev environment.
_PAYLOAD_CAPABLE_FIELD_NAMES = {"prompt", "messages", "content", "response"}


def _field_lines() -> list[str]:
    lines = []
    for name, field in InferenceReceipt.model_fields.items():
        annotation = getattr(field.annotation, "__name__", str(field.annotation))
        lines.append(f"  {name}: {annotation}")
    return lines


def run_verify_payload_free() -> int:
    field_names = set(InferenceReceipt.model_fields)
    offending = field_names & _PAYLOAD_CAPABLE_FIELD_NAMES

    print("Inferrail receipt schema: payload field-name check")
    print("=" * 56)
    print()
    print(f"Model: inferrail.receipts.schema.InferenceReceipt (installed version {_version()})")
    print()
    print("Every field this schema can ever hold, introspected from the running code")
    print("(not a hardcoded list):")
    print()
    for line in _field_lines():
        print(line)
    print()
    if offending:
        # Unreachable in a real release — the equivalent regression test
        # would already fail CI — but this command must never assert
        # something the schema doesn't actually prove, so it checks live
        # rather than trusting its own docstring.
        print(f"FAIL: found payload-capable field(s): {sorted(offending)}")
        return 1

    print(
        "RESULT: PASS (field-name check). No receipt field is named prompt, messages,\n"
        "content, or response. The same check runs in CI as\n"
        "test_inference_receipt_has_no_payload_fields in tests/unit/test_receipts.py."
    )
    print()
    print(
        "What this does not prove:\n"
        "  - Several fields are free-form strings. `attributes` is a dict[str, str] stored\n"
        "    exactly as the caller sends it (X-Inferrail-Attribute-* headers, -a flags).\n"
        "    Keep secrets and message content out of attribute values.\n"
        "  - It inspects the schema, not the data already on disk, your logs, or any\n"
        "    telemetry sink you configure.\n"
        "  - It is not a security audit or certification."
    )
    print()
    print(
        "Behavioral evidence that the gateway's receipt path does not copy request or\n"
        "response bodies: the canary tests in tests/unit/test_gateway_receipts.py,\n"
        "tests/unit/test_gateway.py, and tests/unit/test_gateway_anthropic.py.\n"
        "\n"
        "Scope: this is about the records Inferrail writes, not the network path. Prompts\n"
        "and responses still travel to your configured upstream provider (OpenAI,\n"
        "Anthropic, etc.) exactly as they would without Inferrail in the path. Inferrail\n"
        "is a pass-through gateway to that provider, not a privacy boundary against it."
    )
    return 0


def _version() -> str:
    from inferrail import __version__

    return __version__
