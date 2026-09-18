"""`inferrail verify-payload-free` — see `inferrail.cli.verify`."""

from __future__ import annotations

import pytest

from inferrail.cli.verify import run_verify_payload_free


def test_verify_payload_free_passes_and_lists_every_field(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = run_verify_payload_free()

    assert result == 0
    out = capsys.readouterr().out
    assert "RESULT: PASS" in out
    assert "Suitable for pasting into a security review" in out
    # Every real InferenceReceipt field name should be listed, derived at
    # runtime, not hardcoded.
    for field_name in (
        "receipt_id",
        "request_id",
        "route",
        "provider",
        "model",
        "status",
        "prompt_tokens",
        "completion_tokens",
        "pricing",
        "estimated_cost_usd",
        "attributes",
    ):
        assert field_name in out


def test_verify_payload_free_states_the_pass_through_scope_honestly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_verify_payload_free()

    out = capsys.readouterr().out
    assert "still travel to your configured upstream provider" in out
    assert "not a privacy boundary against it" in out
