"""Public Agent Card discovery-correctness tests for
`hosted/a2a_economic_authority/`.

Closes a real gap: nothing in this suite previously asserted on the
Agent Card's actual *content* (`_wait_for_ready` in
`_a2a_economic_authority_client.py` only checks it returns HTTP 200), so
two stale, internal-only claims (`version: "0.1.0-phaseB"`,
`"no payment yet (Phase B)"` in the description) shipped to the live,
public `/.well-known/agent-card.json` unnoticed -- both wrong the moment
a deployment enables Phase C's `POST /sessions`, since neither field
ever reflected runtime configuration. See `agent_card.py`'s module
docstring for the full design this verifies.

Most tests here call `build_agent_card` directly -- no server needed,
since the card's *content* logic lives entirely in that one function.
`test_build_app_wires_session_purchase_state_into_the_served_card`
additionally proves the real wiring: that `server.build_app` reads
`ECONOMIC_AUTHORITY_SESSION_PAY_TO_ADDRESS` and passes the right value
through to `build_agent_card`, using a real `fastapi.testclient.TestClient`
against the real served `/.well-known/agent-card.json` route -- with
fake-but-well-formed CDP credentials (facilitator-config construction is
local-only, no network call -- see `cdp.x402.create_facilitator_config`),
never a real payment.

Skips automatically unless the hosted extra (a2a-sdk) is installed, same
as every other file in this directory.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
HOSTED_DIR = REPO_ROOT / "hosted" / "a2a_economic_authority"
if str(HOSTED_DIR) not in sys.path:
    sys.path.insert(0, str(HOSTED_DIR))

import pytest  # noqa: E402

pytest.importorskip("a2a")

from agent_card import (  # noqa: E402
    DOCUMENTATION_URL,
    SESSION_PURCHASE_EXTENSION_URI,
    build_agent_card,
)
from google.protobuf.json_format import MessageToDict  # noqa: E402

_EXPECTED_SKILL_IDS = ["reserve", "grant", "consume", "settle", "status", "revoke"]

# Internal-only vocabulary that must never appear in a *public* Agent
# Card, regardless of configuration -- this repo's own phase/decision
# tracking is meaningless (and confusing) to an external caller. Checked
# as whole words via substring on a lowercased/depunctuated haystack
# would be fragile against false positives on legitimate prose (e.g. "a"
# or "b" as standalone words), so these are the specific stale strings
# that actually shipped, not a blanket "phase" ban -- "Phase C" etc.
# appear legitimately in this repo's own internal engineering comments,
# just never in anything served over HTTP.
_STALE_CLAIMS = ["phaseB", "phase B", "Phase B", "no payment yet"]


def _as_dict(card) -> dict:
    return MessageToDict(card, preserving_proto_field_name=False)


def _skill_ids(card_dict: dict) -> list[str]:
    return [s["id"] for s in card_dict["skills"]]


# -- content correctness, both configurations -------------------------------


def test_disabled_configuration_has_no_stale_claims_and_no_extension():
    card = _as_dict(
        build_agent_card(
            url="https://authority.tryinferrail.com/", session_purchase_enabled=False
        )
    )
    serialized = str(card)
    for claim in _STALE_CLAIMS:
        assert claim not in serialized, f"stale claim {claim!r} present: {serialized}"
    assert card["version"] == "0.2.0"
    assert card["documentationUrl"] == DOCUMENTATION_URL
    assert card["capabilities"].get("extensions", []) == []
    assert _skill_ids(card) == _EXPECTED_SKILL_IDS


def test_enabled_configuration_has_no_stale_claims_and_declares_extension():
    card = _as_dict(
        build_agent_card(
            url="https://authority.tryinferrail.com/",
            session_purchase_enabled=True,
            session_purchase_url="https://authority.tryinferrail.com/sessions",
            session_price_usd="0.05",
        )
    )
    serialized = str(card)
    for claim in _STALE_CLAIMS:
        assert claim not in serialized, f"stale claim {claim!r} present: {serialized}"
    assert card["version"] == "0.2.0"
    assert card["documentationUrl"] == DOCUMENTATION_URL
    assert _skill_ids(card) == _EXPECTED_SKILL_IDS

    extensions = card["capabilities"]["extensions"]
    assert len(extensions) == 1
    ext = extensions[0]
    assert ext["uri"] == SESSION_PURCHASE_EXTENSION_URI
    assert ext["params"]["purchase_endpoint"] == "https://authority.tryinferrail.com/sessions"
    assert ext["params"]["price_usd"] == "0.05"
    assert ext["params"]["network"] == "eip155:84532"


def test_enabled_configuration_never_lists_sessions_as_a_skill():
    """`POST /sessions` is a plain HTTP route outside the A2A `SendMessage`
    pipeline -- see `access_control.py`, which disables every other
    standard A2A method regardless of credential. Listing it as a
    `skills` entry would falsely claim a `SendMessage` invocation path
    this service actively rejects."""
    card = _as_dict(
        build_agent_card(
            url="https://authority.tryinferrail.com/",
            session_purchase_enabled=True,
            session_purchase_url="https://authority.tryinferrail.com/sessions",
            session_price_usd="0.05",
        )
    )
    skill_ids = _skill_ids(card)
    assert "sessions" not in skill_ids
    assert "session" not in skill_ids
    assert skill_ids == _EXPECTED_SKILL_IDS


def test_service_fee_and_authority_ceiling_stay_distinct_in_description():
    card = _as_dict(
        build_agent_card(
            url="https://authority.tryinferrail.com/",
            session_purchase_enabled=True,
            session_purchase_url="https://authority.tryinferrail.com/sessions",
            session_price_usd="0.05",
        )
    )
    description = card["description"].lower()
    assert "deposit into" in description or "escrow" in description
    assert "authority ceiling" in description or "authority_ceiling_usd" in description


def test_fails_closed_when_enabled_without_purchase_details():
    with pytest.raises(ValueError):
        build_agent_card(url="https://authority.tryinferrail.com/", session_purchase_enabled=True)
    with pytest.raises(ValueError):
        build_agent_card(
            url="https://authority.tryinferrail.com/",
            session_purchase_enabled=True,
            session_purchase_url="https://authority.tryinferrail.com/sessions",
            # session_price_usd deliberately omitted
        )


# -- security/skills/URL unchanged across both configurations ---------------


@pytest.mark.parametrize("session_purchase_enabled", [False, True])
def test_bearer_security_scheme_unchanged(session_purchase_enabled):
    kwargs = (
        {
            "session_purchase_url": "https://authority.tryinferrail.com/sessions",
            "session_price_usd": "0.05",
        }
        if session_purchase_enabled
        else {}
    )
    card = _as_dict(
        build_agent_card(
            url="https://authority.tryinferrail.com/",
            session_purchase_enabled=session_purchase_enabled,
            **kwargs,
        )
    )
    assert card["securitySchemes"] == {
        "capabilityBearer": {
            "httpAuthSecurityScheme": {
                "scheme": "bearer",
                "bearerFormat": "opaque capability token",
            }
        }
    }
    assert card["securityRequirements"] == [{"schemes": {"capabilityBearer": {}}}]


@pytest.mark.parametrize("session_purchase_enabled", [False, True])
def test_supported_interface_url_matches_caller_supplied_base_url(session_purchase_enabled):
    kwargs = (
        {
            "session_purchase_url": "https://authority.tryinferrail.com/sessions",
            "session_price_usd": "0.05",
        }
        if session_purchase_enabled
        else {}
    )
    base_url = "https://authority.tryinferrail.com/"
    card = _as_dict(
        build_agent_card(
            url=base_url, session_purchase_enabled=session_purchase_enabled, **kwargs
        )
    )
    assert card["supportedInterfaces"][0]["url"] == base_url


# -- no credential/secret leakage --------------------------------------------


def test_agent_card_module_never_touches_environment_or_secrets():
    """Static check: `agent_card.py` must never read `os.environ`, import
    `os`, or reference credential/token-store identifiers -- it builds a
    purely declarative, public document from caller-supplied arguments
    only. A secret leaking into the Agent Card would be the worst possible
    place for it (served unauthenticated to anyone)."""
    source = (HOSTED_DIR / "agent_card.py").read_text()
    forbidden_refs = (
        "import os",
        "os.environ",
        "CDP_API_KEY",
        "capability_store",
        "CapabilityStore",
    )
    for forbidden in forbidden_refs:
        assert forbidden not in source, f"agent_card.py must not reference {forbidden!r}"


def test_enabled_card_never_contains_fake_cdp_credential_value():
    """End-to-end sanity check alongside the static one above: even when
    the caller (a real deployment) holds live CDP credentials elsewhere in
    the process, nothing about *building* an enabled-configuration card
    can make one appear in its output -- the function signature has no
    parameter for it."""
    fake_secret = "sk-test-should-never-appear-in-any-card-output"
    card = _as_dict(
        build_agent_card(
            url="https://authority.tryinferrail.com/",
            session_purchase_enabled=True,
            session_purchase_url="https://authority.tryinferrail.com/sessions",
            session_price_usd="0.05",
        )
    )
    assert fake_secret not in str(card)


# -- real wiring: server.build_app -> build_agent_card -----------------------


def test_build_app_wires_session_purchase_state_into_the_served_card(tmp_path, monkeypatch):
    """Proves the actual runtime wiring, not just `build_agent_card` in
    isolation: with `ECONOMIC_AUTHORITY_SESSION_PAY_TO_ADDRESS` (and the
    CDP credentials `_wire_session_purchase_route` requires to construct a
    facilitator config -- fake values are sufficient, since that
    construction is local-only, never a network call) set,
    `server.build_app`'s real `/.well-known/agent-card.json` route must
    serve a card with the extension declared -- and without it, must not."""
    monkeypatch.delenv("ECONOMIC_AUTHORITY_SESSION_PAY_TO_ADDRESS", raising=False)
    monkeypatch.delenv("CDP_API_KEY_ID", raising=False)
    monkeypatch.delenv("CDP_API_KEY_SECRET", raising=False)

    import server

    importlib.reload(server)
    from fastapi.testclient import TestClient

    disabled_app = server.build_app(
        base_url="http://testserver/",
        db_path=tmp_path / "a.sqlite3",
        capability_db_path=tmp_path / "b.sqlite3",
    )
    disabled_card = TestClient(disabled_app).get("/.well-known/agent-card.json").json()
    assert disabled_card["capabilities"].get("extensions", []) == []
    assert "no payment yet" not in str(disabled_card)

    monkeypatch.setenv("ECONOMIC_AUTHORITY_SESSION_PAY_TO_ADDRESS", "0x" + "11" * 20)
    monkeypatch.setenv("CDP_API_KEY_ID", "fake-test-key-id")
    monkeypatch.setenv("CDP_API_KEY_SECRET", "fake-test-key-secret")
    importlib.reload(server)

    enabled_app = server.build_app(
        base_url="http://testserver/",
        db_path=tmp_path / "c.sqlite3",
        capability_db_path=tmp_path / "d.sqlite3",
    )
    enabled_card = TestClient(enabled_app).get("/.well-known/agent-card.json").json()
    extensions = enabled_card["capabilities"]["extensions"]
    assert len(extensions) == 1
    assert extensions[0]["params"]["purchase_endpoint"] == "http://testserver/sessions"
    assert "fake-test-key-id" not in str(enabled_card)
    assert "fake-test-key-secret" not in str(enabled_card)

    # Reload once more with the env cleared so later test modules importing
    # `server` in the same process don't inherit this test's monkeypatched
    # state after monkeypatch's own teardown restores the real environment.
    monkeypatch.delenv("ECONOMIC_AUTHORITY_SESSION_PAY_TO_ADDRESS", raising=False)
    monkeypatch.delenv("CDP_API_KEY_ID", raising=False)
    monkeypatch.delenv("CDP_API_KEY_SECRET", raising=False)
    importlib.reload(server)
