from __future__ import annotations

import pytest

from inferrail.cli.attributes import parse_cli_attributes


def test_no_attributes_gives_empty_dict() -> None:
    assert parse_cli_attributes(customer=None, workflow=None, attribute_args=[]) == {}


def test_customer_and_workflow_convenience_flags() -> None:
    attrs = parse_cli_attributes(customer="acme", workflow="research", attribute_args=[])

    assert attrs == {"customer": "acme", "workflow": "research"}


def test_generic_attribute_flag() -> None:
    attrs = parse_cli_attributes(
        customer=None, workflow=None, attribute_args=["environment=prod", "feature=x"]
    )

    assert attrs == {"environment": "prod", "feature": "x"}


def test_convenience_and_generic_attributes_combine() -> None:
    attrs = parse_cli_attributes(
        customer="acme", workflow=None, attribute_args=["environment=prod"]
    )

    assert attrs == {"customer": "acme", "environment": "prod"}


def test_malformed_attribute_missing_equals_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid --attribute 'foo'"):
        parse_cli_attributes(customer=None, workflow=None, attribute_args=["foo"])


def test_malformed_attribute_empty_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid --attribute"):
        parse_cli_attributes(customer=None, workflow=None, attribute_args=["=acme"])


def test_malformed_attribute_empty_value_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid --attribute"):
        parse_cli_attributes(customer=None, workflow=None, attribute_args=["customer="])


def test_empty_customer_is_rejected() -> None:
    with pytest.raises(ValueError, match="--customer requires a non-empty value"):
        parse_cli_attributes(customer="", workflow=None, attribute_args=[])


def test_empty_workflow_is_rejected() -> None:
    with pytest.raises(ValueError, match="--workflow requires a non-empty value"):
        parse_cli_attributes(customer=None, workflow="", attribute_args=[])


def test_duplicate_attribute_via_two_generic_flags_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate attribute 'customer'"):
        parse_cli_attributes(
            customer=None, workflow=None, attribute_args=["customer=acme", "customer=globex"]
        )


def test_duplicate_attribute_via_convenience_and_generic_flag_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate attribute 'customer'"):
        parse_cli_attributes(
            customer="acme", workflow=None, attribute_args=["customer=globex"]
        )
