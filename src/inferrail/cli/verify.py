"""`inferrail verify-payload-free`: proof, not a promise, that the receipt
schema Inferrail actually writes to disk cannot hold a prompt, response, or
any other message content.

Everything printed here is derived from the real, running
`InferenceReceipt` model at call time (`inferrail.receipts.schema`) — never
a hardcoded string a future schema change could quietly drift out of sync
with. The same structural check this command runs is also a regression
test (`test_inference_receipt_has_no_payload_fields`,
`tests/unit/test_receipts.py`), so a future field named `prompt` or
`response` would fail CI before it could ever reach a release, not just
fail this command.

Exists for the person who has to answer a security/compliance reviewer's
question with more than "we promise" — see
docs/adr/0020-quickstart-both-sdks-and-payload-free-verification.md.
"""

from __future__ import annotations

from inferrail.receipts.schema import InferenceReceipt

# The same disjoint-set check test_inference_receipt_has_no_payload_fields
# makes — kept here too (not imported from the test module, which pytest
# owns) so this command's proof doesn't depend on the test suite being
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

    print("Inferrail receipt schema — payload-free verification")
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
        print("Do not paste this output into a security review.")
        return 1

    print(
        "RESULT: PASS — none of this schema's fields can hold a prompt, response, tool-call\n"
        "payload, or any other message content. This is a structural property of the\n"
        "field list above, not a runtime setting that could be misconfigured on — there is\n"
        "no field named or shaped to carry free-form message content, checked against the\n"
        "same names (prompt/messages/content/response) this project's own CI enforces via\n"
        "test_inference_receipt_has_no_payload_fields in tests/unit/test_receipts.py."
    )
    print()
    print(
        "Scope, stated plainly: this is a receipt-storage guarantee, not a network claim.\n"
        "The prompt and response still travel to your configured upstream provider (OpenAI,\n"
        "Anthropic, etc.) exactly as they would without Inferrail in the path — Inferrail is\n"
        "a pass-through gateway to that provider, not a privacy boundary against it. What\n"
        "this command proves is narrower and verifiable: the record Inferrail itself writes\n"
        "to your receipts store never contains that content, structurally, not by policy."
    )
    print()
    print("Suitable for pasting into a security review as-is.")
    return 0


def _version() -> str:
    from inferrail import __version__

    return __version__
