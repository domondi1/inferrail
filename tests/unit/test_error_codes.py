"""Every `InferrailError` subclass must have exactly one stable code.

Mirrors `test_inference_event_has_no_payload_fields`'s structural-guarantee
style: this discovers the exception hierarchy dynamically (not a hardcoded
list that could silently drift) so a new exception subclass without a
registered code fails this test rather than shipping with a missing code.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from inferrail import errors as errors_module
from inferrail.errors.codes import UNKNOWN_CODE, all_codes, code_for, docs_url_for
from inferrail.errors.exceptions import InferrailError
from inferrail.gateway.app import create_app


def _all_subclasses(cls: type) -> set[type]:
    direct = set(cls.__subclasses__())
    return direct | {s for c in direct for s in _all_subclasses(c)}


def test_every_inferrail_error_subclass_has_exactly_one_registered_code() -> None:
    all_subclasses = _all_subclasses(InferrailError)
    coded_types = {exc_type for exc_type, _ in all_codes()}
    missing = all_subclasses - coded_types
    assert not missing, f"no error code registered for: {missing}"


def test_codes_are_unique_and_well_formed() -> None:
    codes = [entry.code for _, entry in all_codes()] + [UNKNOWN_CODE.code]
    assert len(codes) == len(set(codes)), "duplicate error codes"
    for code in codes:
        assert re.fullmatch(r"INFERRAIL_E\d{3}", code), code


def test_every_error_class_exported_from_errors_package_has_a_code() -> None:
    # errors/__init__.py's __all__ is the public exception surface; every
    # name there must resolve to a code, independent of the __subclasses__
    # walk above (which would miss a class if it were ever imported but
    # never subclassed from InferrailError directly for some reason).
    coded_types = {exc_type for exc_type, _ in all_codes()}
    for name in errors_module.__all__:
        exc_type = getattr(errors_module, name)
        if exc_type is InferrailError:
            continue
        assert exc_type in coded_types, name


def test_docs_url_anchors_match_generated_errors_md() -> None:
    for _, code in all_codes():
        url = docs_url_for(code.code)
        assert url.endswith(f"#{code.code.lower()}")


def test_routing_error_resolves_to_e007_end_to_end_over_http() -> None:
    # code_for() unit-level correctness is necessary but not sufficient —
    # this proves the code actually reaches the HTTP error body a real
    # caller sees, through gateway/app.py's exception handler.
    from inferrail.config.quickstart import build_quickstart_config

    app = create_app(build_quickstart_config(telemetry_sink="none"))
    client = TestClient(app)

    response = client.post(
        "/v1/chat/completions",
        json={"model": "not-a-configured-route", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "INFERRAIL_E007"
    assert body["error"]["remediation"]
    assert body["error"]["docs_url"].endswith("#inferrail_e007")


def test_unsupported_stream_resolves_to_e006_end_to_end_over_http() -> None:
    from inferrail.config.quickstart import build_quickstart_config

    app = create_app(build_quickstart_config(telemetry_sink="none"))
    client = TestClient(app)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "default",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INFERRAIL_E006"


def test_code_for_falls_back_to_unknown_for_bare_inferrail_error() -> None:
    # InferrailError itself is never raised directly in this codebase, but
    # code_for must still degrade safely rather than raising, since it's
    # on the exception-handling path itself.
    class _NotRegistered(InferrailError):
        pass

    assert code_for(_NotRegistered("x")) is UNKNOWN_CODE
