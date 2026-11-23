"""Does an x402 offer make us send the facilitator a request its published API accepts?

A buyer pays an x402 offer by echoing parts of it in the payment: the resource (url,
description, mimeType), the accepted payment requirements and the discovery extensions. The
server forwards them to the CDP facilitator's `/v2/x402/verify` and `/v2/x402/settle`. The
facilitator validates that body against its OpenAPI schema first and refuses the whole request
(HTTP 400, an API error with a correlationId) when any field breaks the schema, however good the
payment. On 2026-11-23 a 501-character `resource.description` (the schema allows 500) did that to
every Web Read buyer while the offer itself still passed Coinbase's public offer validator.

This checks an offer against the facilitator's own schema, the generated models of its OpenAPI
that ship in `cdp-sdk` (already a production dependency). A test runs it on every paid route and
the pre-deploy check runs it on the offers the deploy would serve. It makes no network call,
signs nothing and pays nothing: the payment part of the body is a well-formed placeholder.
"""

from __future__ import annotations

import base64
import json
from typing import Any

# A well-formed but meaningless EIP-3009 authorization: only the shape of the body is checked.
_PLACEHOLDER_PAYER = "0x" + "1" * 40
_PLACEHOLDER_SIGNATURE = "0x" + "0" * 130
_PLACEHOLDER_NONCE = "0x" + "0" * 64


def decode_payment_required(header: str) -> dict[str, Any]:
    """The JSON of a `PAYMENT-REQUIRED` header (base64 of the 402's payment requirements)."""
    return json.loads(base64.b64decode(header))


def _drop_none(value: Any) -> Any:
    return {k: v for k, v in value.items() if v is not None} if isinstance(value, dict) else value


def verify_body(required: dict[str, Any], accepted: dict[str, Any]) -> dict[str, Any]:
    """The `/verify` (and `/settle`) body a buyer's `exact` EVM payment of `accepted` makes the
    server send: every field echoed from the offer is the offer's own; only the signature and
    authorization are placeholders."""
    version = required.get("x402Version", 2)
    payload = {
        "x402Version": version,
        "payload": {
            "signature": _PLACEHOLDER_SIGNATURE,
            "authorization": {
                "from": _PLACEHOLDER_PAYER,
                "to": accepted.get("payTo"),
                "value": accepted.get("amount"),
                "validAfter": "0",
                "validBefore": "9999999999",
                "nonce": _PLACEHOLDER_NONCE,
            },
        },
        "accepted": accepted,
        "resource": required.get("resource"),
        "extensions": required.get("extensions"),
    }
    return {"x402Version": version, "paymentPayload": _drop_none(payload),
            "paymentRequirements": accepted}  # fmt: skip


def body_problems(body: dict[str, Any]) -> list[str]:
    """Why the facilitator's published schema would refuse this verify/settle body (empty = it
    accepts it). Raises ImportError when cdp-sdk is missing: no check is not a pass."""
    from cdp.openapi_client.models.verify_x402_payment_request import VerifyX402PaymentRequest

    try:
        if VerifyX402PaymentRequest.from_dict(body) is None:
            return ["the facilitator schema read no request from the body"]
    except Exception as ex:  # pydantic ValidationError / ValueError from the generated oneOf
        text = " ".join(str(ex).split())
        cut = text.find("validation error for ")
        return [text[cut:cut + 300] if cut >= 0 else text[:300]]
    return []


def offer_problems(required: dict[str, Any]) -> list[str]:
    """Why paying this 402 would get a schema refusal from the facilitator, per accepted option.
    Options other than `exact` on an EVM network aren't covered by this check, so they are
    reported rather than passed."""
    problems: list[str] = []
    for accepted in required.get("accepts") or []:
        where = f"{accepted.get('scheme')} on {accepted.get('network')}"
        if accepted.get("scheme") != "exact" or not str(accepted.get("network")).startswith(
            "eip155:"
        ):
            problems.append(f"{where}: no facilitator contract check for this option")
            continue
        problems += [f"{where}: {p}" for p in body_problems(verify_body(required, accepted))]
    if not required.get("accepts"):
        problems.append("the offer has no payment options")
    return problems


def description_limit() -> int:
    """The longest `resource.description` the facilitator's schema accepts (500 in cdp-sdk
    1.48.1), read from the schema so a change there moves the limit our tests hold us to."""
    from cdp.openapi_client.models.x402_resource_info import X402ResourceInfo

    for meta in X402ResourceInfo.model_fields["description"].metadata:
        if getattr(meta, "max_length", None) is not None:
            return int(meta.max_length)
    for arg in getattr(X402ResourceInfo.model_fields["description"].annotation, "__args__", ()):
        for meta in getattr(arg, "__metadata__", ()):
            for m in getattr(meta, "metadata", [meta]):
                if getattr(m, "max_length", None) is not None:
                    return int(m.max_length)
    raise LookupError("the facilitator schema no longer states a description limit")
