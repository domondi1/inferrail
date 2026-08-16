"""Caller-supplied business attribution: the generic, non-vertical-specific
mechanism for attaching context (customer, workflow, tenant, feature,
environment, project, ...) to a request.

Deliberately transported as HTTP headers, not a JSON body field: the OpenAI
`/v1/chat/completions` request body stays byte-for-byte what an unmodified
OpenAI client sends (see docs/adr/0001), and the header value never enters
`NormalizedChatRequest` — the type `providers.openai.OpenAIProvider.complete`
consumes — so attribution data is structurally incapable of being forwarded
to the upstream provider; there is no stripping step to forget.

Attribute values ARE persisted, verbatim, in `InferenceReceipt.attributes`
(see receipts/schema.py). Do not put secrets or other sensitive data in
them — see README.md's attribution section.
"""

from __future__ import annotations

from collections.abc import Mapping

_HEADER_PREFIX = "x-inferrail-attribute-"


def extract_attributes(headers: Mapping[str, str]) -> dict[str, str]:
    """Collect `X-Inferrail-Attribute-<Name>` headers into `{name: value}`.

    Header names are case-insensitive (per HTTP) and matched
    case-insensitively here; the resulting key is the lowercased,
    hyphen-to-underscore-normalized suffix, e.g.
    `X-Inferrail-Attribute-Workflow-Type` -> `workflow_type`. A header with
    an empty name suffix or an empty value is dropped rather than stored,
    since neither carries any attributable meaning.
    """
    attributes: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        lowered = raw_name.lower()
        if not lowered.startswith(_HEADER_PREFIX):
            continue
        key = lowered[len(_HEADER_PREFIX) :].replace("-", "_")
        if not key or not raw_value:
            continue
        attributes[key] = raw_value
    return attributes
