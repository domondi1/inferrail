"""Turns `inferrail try`'s attribution flags into the same
`dict[str, str]` primitive the gateway extracts from
`X-Inferrail-Attribute-*` headers (see `gateway/attribution.py`).

There is exactly one attribution model in Inferrail; this and
`gateway/attribution.py` are just its two entry points (CLI flags vs. HTTP
headers). `--customer`/`--workflow` are convenience shorthand for the
generic `-a/--attribute KEY=VALUE` form, not a separate concept.
"""

from __future__ import annotations


def parse_cli_attributes(
    *, customer: str | None, workflow: str | None, attribute_args: list[str]
) -> dict[str, str]:
    """Build the attributes dict for one `inferrail try` invocation.

    Raises `ValueError` (message is already user-facing) on a malformed
    `-a` entry, an empty `--customer`/`--workflow` value, or the same
    attribute key supplied more than once.
    """
    attributes: dict[str, str] = {}

    def _add(key: str, value: str, *, source: str) -> None:
        if not value:
            raise ValueError(f"{source} requires a non-empty value")
        if key in attributes:
            raise ValueError(f"duplicate attribute '{key}' (from {source})")
        attributes[key] = value

    if customer is not None:
        _add("customer", customer, source="--customer")
    if workflow is not None:
        _add("workflow", workflow, source="--workflow")
    for raw in attribute_args:
        key, sep, value = raw.partition("=")
        if not sep or not key or not value:
            raise ValueError(
                f"invalid --attribute '{raw}': expected KEY=VALUE with a non-empty key and value"
            )
        _add(key, value, source=f"--attribute {key}")

    return attributes
