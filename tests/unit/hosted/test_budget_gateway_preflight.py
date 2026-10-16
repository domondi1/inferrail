"""The pre-deploy payment check stops a deploy that would crash the shared service at startup or
collect payments differently from what was approved, and never prints secret values."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

HOSTED = Path(__file__).resolve().parents[3] / "hosted" / "budget_gateway"
APPROVED = "0x5a5A5a5a5A5a5a5a5a5A5a5A5A5a5a5A5A5A5A5A"
SECRET = "very-secret-api-key-value"


@pytest.fixture
def preflight(tmp_path):
    sys.path.insert(0, str(HOSTED))
    spec = importlib.util.spec_from_file_location("hosted_preflight", HOSTED / "preflight.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _env(tmp_path, **extra):
    return {"BG_DATA_DIR": str(tmp_path / "data"), **extra}


def _mainnet(tmp_path, **extra):
    base = {
        "BG_X402_PAY_TO": APPROVED,
        "BG_X402_NETWORK": "eip155:8453",
        "BG_X402_MAINNET_APPROVED": "1",
        "BG_BASE_RPC_URL": "https://rpc.example",
        "CDP_API_KEY_ID": "key-id",
        "CDP_API_KEY_SECRET": SECRET,
    }
    return _env(tmp_path, **{**base, **extra})


def _run(preflight, env, expect=APPROVED, chain="0x2105", kinds=None):
    return preflight.check(
        env,
        expect,
        chain_id=lambda url: chain,
        kinds=lambda e: kinds if kinds is not None else {("exact", "eip155:8453")},
        build=lambda settings: None,
    )


def test_payments_off_passes_without_an_expected_address(preflight, tmp_path, capsys):
    assert _run(preflight, _env(tmp_path), expect=None) == []
    assert "payments: OFF" in capsys.readouterr().out


def test_the_approved_mainnet_configuration_passes(preflight, tmp_path, capsys):
    assert _run(preflight, _mainnet(tmp_path)) == []
    out = capsys.readouterr().out
    assert APPROVED in out and "chain id 0x2105" in out and "exact on eip155:8453" in out
    assert SECRET not in out


@pytest.mark.parametrize(
    ("drop", "set_", "needle"),
    [
        ("BG_X402_MAINNET_APPROVED", {}, "would not start"),
        ("BG_BASE_RPC_URL", {}, "would not start"),
        ("CDP_API_KEY_SECRET", {}, "mainnet needs CDP_API_KEY_SECRET"),
        (None, {"BG_X402_PAY_TO": "0x" + "1" * 40}, "not the approved"),
        (None, {"CDP_WALLET_SECRET": SECRET}, "CDP_WALLET_SECRET is set"),
        (None, {"BG_X402_NETWORK": "eip155:1"}, "would not start"),
        (None, {"BG_MOUNT_PREFIX": "/elsewhere"}, "would not start"),
    ],
)
def test_unsafe_configurations_stop_the_deploy(preflight, tmp_path, drop, set_, needle):
    env = _mainnet(tmp_path, **set_)
    if drop:
        env.pop(drop)
    problems = _run(preflight, env)
    assert any(needle in p for p in problems), problems
    assert all(SECRET not in p for p in problems)


def test_wrong_chain_or_unsupported_facilitator_stops_the_deploy(preflight, tmp_path):
    assert any("not eip155:8453" in p for p in _run(preflight, _mainnet(tmp_path), chain="0x1"))
    assert any(
        "does not support exact" in p
        for p in _run(preflight, _mainnet(tmp_path), kinds={("exact", "eip155:84532")})
    )


def test_payments_on_without_an_expected_address_stops_the_deploy(preflight, tmp_path):
    assert any("no --expect-pay-to" in p for p in _run(preflight, _mainnet(tmp_path), expect=None))


def test_network_failures_stop_the_deploy_without_leaking(preflight, tmp_path):
    def boom(*_a):
        raise ConnectionError(SECRET)

    problems = preflight.check(
        _mainnet(tmp_path), APPROVED, chain_id=boom, kinds=boom, build=lambda settings: None
    )
    assert len(problems) == 2 and all(SECRET not in p for p in problems)


def test_the_real_app_is_built_with_payments_off_and_on(preflight, tmp_path, capsys):
    """No stubbed builder: the same combined app the service starts, on a throwaway directory.
    "On" uses Base Sepolia (no facilitator key needed) and a burn address."""
    pytest.importorskip("x402")
    assert preflight.check(_env(tmp_path), None) == []
    on = _env(tmp_path, BG_X402_PAY_TO="0x000000000000000000000000000000000000dEaD")
    assert preflight.check(on, "0x000000000000000000000000000000000000dEaD") == []
    assert capsys.readouterr().out.count("the application builds") == 2


def test_a_startup_import_error_stops_the_deploy(preflight, tmp_path):
    """The production failure this guards against: an import that only runs when the credit
    rail is on, missing from the production install."""

    def missing_extra(settings):
        raise ImportError(
            "Extensions validation requires jsonschema. Install with: pip install x402[extensions]"
        )

    problems = preflight.check(
        _mainnet(tmp_path),
        APPROVED,
        chain_id=lambda u: "0x2105",
        kinds=lambda e: set(),
        build=missing_extra,
    )
    assert problems == [
        "the application would not start: ImportError: Extensions validation requires "
        "jsonschema. Install with: pip install x402[extensions]"
    ]


# -- Job-Safe Search on the shared host ------------------------------------------------------
SEARCH_PAY_TO = "0x" + "2" * 40


def _search(tmp_path, **extra):
    base = {
        "SEARCH_PAY_TO": SEARCH_PAY_TO,
        "SEARCH_RESOURCE_URL": "https://host.example/job-search/search",
        "SEARCH_TOKEN_SECRET": "s" * 40,
        "SEARCH_RPC_URL": "https://sepolia.base.org",
        "SEARCH_SUPPLIER": "fixture",
    }
    return _env(tmp_path, **{**base, **extra})


def _check_search(preflight, env, expect_search=None):
    return preflight.check(env, None, build=lambda s: None, expect_search_pay_to=expect_search)


def test_search_on_the_shared_host_is_built_with_its_own_checks(preflight, tmp_path, capsys):
    pytest.importorskip("x402")
    pytest.importorskip("cdp")
    assert _check_search(preflight, _search(tmp_path)) == []
    assert "search: builds; network eip155:84532" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("extra", "needle"),
    [
        # Search's own startup refusals, surfaced before the deploy instead of at startup.
        ({"SEARCH_NETWORK": "eip155:8453"}, "mainnet_requires"),
        ({"SEARCH_TOKEN_SECRET": "short"}, "token_secret_must_be_at_least_32_bytes"),
        ({"SEARCH_RESOURCE_URL": "https://host.example/search"}, "requires_job_search_prefix"),
    ],
)
def test_unsafe_search_configurations_stop_the_deploy(preflight, tmp_path, extra, needle):
    pytest.importorskip("x402")
    pytest.importorskip("cdp")
    problems = _check_search(preflight, _search(tmp_path, **extra))
    assert len(problems) == 1 and "Job-Safe Search would not start" in problems[0]
    assert needle in problems[0], problems


def test_search_and_hosted_must_not_share_a_receiving_address(preflight, tmp_path):
    pytest.importorskip("x402")
    pytest.importorskip("cdp")
    env = _search(tmp_path, BG_X402_PAY_TO=SEARCH_PAY_TO)  # both on Base Sepolia
    problems = preflight.check(
        env,
        SEARCH_PAY_TO,
        build=lambda s: None,
        chain_id=lambda u: "0x14a34",
        kinds=lambda e: set(),
    )
    assert any("cohosted_payment_merchants_must_be_distinct" in p for p in problems), problems


def test_search_receiving_address_must_be_the_approved_one(preflight, tmp_path):
    pytest.importorskip("x402")
    pytest.importorskip("cdp")
    problems = _check_search(preflight, _search(tmp_path), expect_search="0x" + "3" * 40)
    assert any("SEARCH_PAY_TO is" in p and "not the approved" in p for p in problems)


def test_without_search_configured_nothing_extra_is_built(preflight, tmp_path):
    def must_not_run(*_a):
        raise AssertionError("search build ran without SEARCH_PAY_TO")

    assert (
        preflight.check(_env(tmp_path), None, build=lambda s: None, build_search=must_not_run) == []
    )


READ = "0x" + "5" * 40


def test_web_read_is_built_and_checked_against_its_approved_address(preflight, tmp_path, capsys):
    pytest.importorskip("x402")  # the hosted CI job installs it and fails on skips
    env = _env(tmp_path, BG_X402_PAY_TO=APPROVED, READ_PAY_TO=READ)
    assert preflight.check(env, APPROVED, build=lambda s: None, expect_read_pay_to=READ) == []
    assert f"web read: builds; network eip155:84532, payTo {READ}, price $0.002" in (
        capsys.readouterr().out
    )
    wrong = preflight.check(env, APPROVED, build=lambda s: None, expect_read_pay_to=APPROVED)
    assert any("READ_PAY_TO is" in p for p in wrong)


def test_web_read_that_would_not_mount_stops_the_deploy(preflight, tmp_path):
    env = _env(tmp_path, BG_X402_PAY_TO=APPROVED, READ_PAY_TO=APPROVED)  # Hosted's own address
    problems = preflight.check(env, APPROVED, build=lambda s: None)
    assert any("Web Read is configured" in p for p in problems)
    assert not (tmp_path / "web-read").exists()  # the check never writes to the real disk
