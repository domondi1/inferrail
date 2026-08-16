from __future__ import annotations

from inferrail.gateway.attribution import extract_attributes


def test_extracts_prefixed_headers() -> None:
    headers = {
        "X-Inferrail-Attribute-Customer": "acme",
        "X-Inferrail-Attribute-Workflow": "contract-review",
    }

    attributes = extract_attributes(headers)

    assert attributes == {"customer": "acme", "workflow": "contract-review"}


def test_header_matching_is_case_insensitive() -> None:
    headers = {"x-inferrail-attribute-customer": "acme"}

    assert extract_attributes(headers) == {"customer": "acme"}


def test_ignores_unrelated_headers() -> None:
    headers = {
        "Authorization": "Bearer secret-token",
        "Content-Type": "application/json",
        "X-Inferrail-Attribute-Customer": "acme",
    }

    attributes = extract_attributes(headers)

    assert attributes == {"customer": "acme"}
    assert "authorization" not in attributes
    assert "secret-token" not in attributes.values()


def test_no_attribution_headers_returns_empty_dict() -> None:
    assert extract_attributes({"Content-Type": "application/json"}) == {}


def test_empty_attribute_name_suffix_is_skipped() -> None:
    headers = {"X-Inferrail-Attribute-": "acme"}

    assert extract_attributes(headers) == {}


def test_empty_attribute_value_is_skipped() -> None:
    headers = {"X-Inferrail-Attribute-Customer": ""}

    assert extract_attributes(headers) == {}


def test_multi_word_attribute_name_normalized_to_underscores() -> None:
    headers = {"X-Inferrail-Attribute-Workflow-Type": "contract-review"}

    assert extract_attributes(headers) == {"workflow_type": "contract-review"}
