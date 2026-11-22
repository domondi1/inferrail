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


POLYGON = "eip155:137"
POLYGON_READ = "0x" + "6" * 40


def _read_extra_env(tmp_path, **extra):
    return _env(
        tmp_path, BG_X402_PAY_TO=APPROVED, READ_PAY_TO=READ,
        READ_X402_EXTRA_NETWORKS=f"{POLYGON}={POLYGON_READ}",
        READ_X402_EXTRA_MAINNET_APPROVED="1", **extra,
    )  # fmt: skip


def test_an_extra_web_read_network_must_be_stated_matched_and_supported(preflight, tmp_path):
    pytest.importorskip("x402")  # the hosted CI job installs it and fails on skips
    env = _read_extra_env(tmp_path)

    def kinds(_env):
        return {("exact", "eip155:84532"), ("exact", POLYGON)}

    good = {POLYGON: POLYGON_READ}
    ok = preflight.check(
        env, APPROVED, build=lambda s: None, kinds=kinds, expect_read_pay_to=READ,
        expect_read_extra=good,
    )  # fmt: skip
    assert ok == []
    missing = preflight.check(env, APPROVED, build=lambda s: None, kinds=kinds,
                              expect_read_pay_to=READ)  # fmt: skip
    assert any("no --expect-read-extra" in p for p in missing)
    wrong = preflight.check(
        env, APPROVED, build=lambda s: None, kinds=kinds, expect_read_pay_to=READ,
        expect_read_extra={POLYGON: APPROVED},
    )  # fmt: skip
    assert any("not the approved" in p and POLYGON in p for p in wrong)
    unsupported = preflight.check(
        env, APPROVED, build=lambda s: None, kinds=lambda _e: {("exact", "eip155:84532")},
        expect_read_pay_to=READ, expect_read_extra={POLYGON: POLYGON_READ},
    )  # fmt: skip
    assert any("does not support exact on eip155:137" in p for p in unsupported)


def test_an_expected_extra_network_that_is_not_configured_stops_the_deploy(preflight, tmp_path):
    pytest.importorskip("x402")
    env = _env(tmp_path, BG_X402_PAY_TO=APPROVED, READ_PAY_TO=READ)
    problems = preflight.check(env, APPROVED, build=lambda s: None, expect_read_pay_to=READ,
                               expect_read_extra={POLYGON: POLYGON_READ})  # fmt: skip
    assert any("doesn't accept" in p for p in problems)


def test_a_mainnet_extra_without_its_approval_does_not_mount(preflight, tmp_path):
    pytest.importorskip("x402")
    env = _read_extra_env(tmp_path)
    env.pop("READ_X402_EXTRA_MAINNET_APPROVED")
    problems = preflight.check(env, APPROVED, build=lambda s: None, expect_read_pay_to=READ)
    assert any("Web Read is configured" in p for p in problems)


def test_web_read_that_would_not_mount_stops_the_deploy(preflight, tmp_path):
    env = _env(tmp_path, BG_X402_PAY_TO=APPROVED, READ_PAY_TO=APPROVED)  # Hosted's own address
    problems = preflight.check(env, APPROVED, build=lambda s: None)
    assert any("Web Read is configured" in p for p in problems)
    assert not (tmp_path / "web-read").exists()  # the check never writes to the real disk


SEARCH = "0x" + "5" * 40
SEARCH_ENV = {
    "WEB_SEARCH_PAY_TO": SEARCH,
    "WEB_SEARCH_SUPPLIER_RIGHTS_CONFIRMED": "1",
    "WEB_SEARCH_SUPPLIER_KEY": "test-key",
    "WEB_SEARCH_SUPPLIER_CREDIT_USD": "0.0008",
    "WEB_SEARCH_MPP_CHAIN_ID": "42431",
    "BG_OPERATOR_TOKEN": "op",
}


def test_web_search_is_built_and_its_address_must_be_stated(preflight, tmp_path, capsys):
    pytest.importorskip("x402")
    env = _env(tmp_path, BG_X402_PAY_TO=APPROVED, **SEARCH_ENV)
    ok = preflight.check(env, APPROVED, build=lambda s: None, expect_web_search_pay_to=SEARCH)
    assert ok == []
    out = capsys.readouterr().out
    assert f"web search: builds; payTo {SEARCH}, price $0.002, rails MPP on Tempo" in out
    unstated = preflight.check(env, APPROVED, build=lambda s: None)
    assert any("--expect-web-search-pay-to" in p for p in unstated)
    wrong = preflight.check(env, APPROVED, build=lambda s: None, expect_web_search_pay_to=APPROVED)
    assert any("WEB_SEARCH_PAY_TO is" in p for p in wrong)
    assert not (tmp_path / "web-search").exists()


def test_web_search_without_supplier_rights_stops_the_deploy(preflight, tmp_path):
    pytest.importorskip("x402")
    env = _env(tmp_path, BG_X402_PAY_TO=APPROVED, **SEARCH_ENV)
    env.pop("WEB_SEARCH_SUPPLIER_RIGHTS_CONFIRMED")
    problems = preflight.check(env, APPROVED, build=lambda s: None, expect_web_search_pay_to=SEARCH)
    assert any("Web Search is configured" in p for p in problems)


# -- x402 Finder: Base pays its own address, Tempo keeps Web Read's ------------------------------
FINDER_BASE = "0x54BF9C490112CD6a54Cb8f22D6bf83621ABe77CF"
WEB_READ = "0xA45140E99E9482a50BC3fA5F6000c02FAAB6F65E"
FINDER = {"FINDER_PAY_TO": WEB_READ, "READ_PAY_TO": WEB_READ, "FINDER_X402": "1",
          "FINDER_X402_PAY_TO": FINDER_BASE, "FINDER_NETWORK": "eip155:8453",
          "FINDER_MAINNET_APPROVED": "1", "FINDER_MPP_CHAIN_ID": "4217",
          "FINDER_MPP_MAINNET_APPROVED": "1", "FINDER_CATALOG_RIGHTS_CONFIRMED": "1",
          "BG_OPERATOR_TOKEN": "op"}  # fmt: skip


@pytest.fixture
def finder_env(monkeypatch):
    pytest.importorskip("x402")  # the plain test job has no hosted dependencies
    # Base mainnet x402 settles through the CDP facilitator, which reads its keys from the
    # process environment; dummy values build the app without contacting anything.
    monkeypatch.setenv("CDP_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("CDP_API_KEY_SECRET", "test-key-secret")


def _finder_run(preflight, env, expect_finder):
    return [p for p in preflight.check(
        env, None, chain_id=lambda url: "0x2105", kinds=lambda e: {("exact", "eip155:8453")},
        build=lambda settings: None, expect_finder_x402_pay_to=expect_finder,
    ) if "Finder" in p or "FINDER" in p]  # fmt: skip


def test_the_approved_finder_recipients_pass(preflight, tmp_path, capsys, finder_env):
    assert _finder_run(preflight, _env(tmp_path, **FINDER), FINDER_BASE) == []
    out = capsys.readouterr().out
    assert f"x402 payTo {FINDER_BASE}" in out and f"MPP payTo {WEB_READ}" in out


@pytest.mark.parametrize(
    ("expect", "needle"),
    [(None, "no --expect-finder-x402-pay-to"), ("0x" + "1" * 40, "not the approved")],
)
def test_a_wrong_or_unstated_finder_base_address_stops_the_deploy(
    preflight, tmp_path, expect, needle, finder_env
):
    problems = _finder_run(preflight, _env(tmp_path, **FINDER), expect)
    assert problems and needle in problems[0]


# -- Domain Check: Base pays its own address, Tempo keeps Web Read's; off unless configured -----
DOMAIN_BASE = "0x" + "D" * 40
DOMAIN = {"DOMAIN_PAY_TO": WEB_READ, "READ_PAY_TO": WEB_READ, "DOMAIN_X402": "1",
          "DOMAIN_X402_PAY_TO": DOMAIN_BASE, "DOMAIN_NETWORK": "eip155:8453",
          "DOMAIN_MAINNET_APPROVED": "1", "DOMAIN_MPP_CHAIN_ID": "4217",
          "DOMAIN_MPP_MAINNET_APPROVED": "1", "BG_OPERATOR_TOKEN": "op"}  # fmt: skip


def _domain_run(preflight, env, expect_domain, expect_read=WEB_READ):
    return [p for p in preflight.check(
        env, None, chain_id=lambda url: "0x2105", kinds=lambda e: {("exact", "eip155:8453")},
        build=lambda settings: None, expect_read_pay_to=expect_read,
        expect_domain_x402_pay_to=expect_domain,
    ) if "Domain" in p or "DOMAIN" in p or "domain" in p]  # fmt: skip


def test_the_approved_domain_recipients_pass(preflight, tmp_path, capsys, finder_env):
    assert _domain_run(preflight, _env(tmp_path, **DOMAIN), DOMAIN_BASE) == []
    out = capsys.readouterr().out
    assert f"x402 payTo {DOMAIN_BASE}" in out and f"MPP payTo {WEB_READ}" in out


@pytest.mark.parametrize(
    ("change", "expect", "needle"),
    [
        ({}, None, "no --expect-domain-x402-pay-to"),
        ({}, "0x" + "1" * 40, "not the approved"),
        ({"DOMAIN_PAY_TO": "0x" + "2" * 40}, DOMAIN_BASE, "not Web Read's approved"),
        ({"DOMAIN_X402": "0", "DOMAIN_X402_PAY_TO": None}, DOMAIN_BASE, "x402 is off"),
        ({"DOMAIN_X402_PAY_TO": FINDER_BASE, "FINDER_X402_PAY_TO": FINDER_BASE}, FINDER_BASE,
         "would not mount"),  # another service's Base address
    ],
)  # fmt: skip
def test_a_wrong_domain_recipient_stops_the_deploy(
    preflight, tmp_path, change, expect, needle, finder_env
):
    env = _env(tmp_path, **DOMAIN)
    for k, v in change.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    problems = _domain_run(preflight, env, expect)
    assert problems and needle in problems[0], problems


def test_domain_check_off_leaves_the_deploy_unchanged(preflight, tmp_path, finder_env):
    # No DOMAIN_PAY_TO: nothing about Domain Check is checked or required.
    assert _domain_run(preflight, _env(tmp_path), None) == []
    stray = _domain_run(preflight, _env(tmp_path), DOMAIN_BASE)
    assert stray and "isn't configured" in stray[0]


def test_mpp_only_domain_check_needs_no_base_address(preflight, tmp_path, finder_env):
    env = _env(tmp_path, **{**DOMAIN, "DOMAIN_X402": "0"})
    env.pop("DOMAIN_X402_PAY_TO")
    assert _domain_run(preflight, env, None) == []


# -- Package Check: checked when configured; its Base address when the flag is given -----------
PKG_BASE = "0xe9c1249648257C1C1D70F8b7682851C377bb8f53"
PKG = {"PKG_PAY_TO": WEB_READ, "READ_PAY_TO": WEB_READ, "PKG_X402": "1",
       "PKG_X402_PAY_TO": PKG_BASE, "PKG_NETWORK": "eip155:8453", "PKG_MAINNET_APPROVED": "1",
       "PKG_MPP_CHAIN_ID": "4217", "PKG_MPP_MAINNET_APPROVED": "1",
       "BG_OPERATOR_TOKEN": "op"}  # fmt: skip


def _pkg_run(preflight, env, expect_pkg, expect_read=WEB_READ):
    return [p for p in preflight.check(
        env, None, chain_id=lambda url: "0x2105", kinds=lambda e: {("exact", "eip155:8453")},
        build=lambda settings: None, expect_read_pay_to=expect_read,
        expect_pkg_x402_pay_to=expect_pkg,
    ) if "Package" in p or "PKG" in p or "pkg" in p]  # fmt: skip


def test_the_approved_package_recipients_pass(preflight, tmp_path, capsys, finder_env):
    assert _pkg_run(preflight, _env(tmp_path, **PKG), PKG_BASE) == []
    out = capsys.readouterr().out
    assert f"x402 payTo {PKG_BASE}" in out and f"MPP payTo {WEB_READ}" in out


def test_an_existing_deploy_command_without_the_flag_still_passes(preflight, tmp_path, finder_env):
    assert _pkg_run(preflight, _env(tmp_path, **PKG), None) == []


@pytest.mark.parametrize(
    ("change", "expect", "needle"),
    [
        ({}, "0x" + "1" * 40, "not the approved"),
        ({"PKG_PAY_TO": "0x" + "2" * 40}, PKG_BASE, "not Web Read's approved"),
        ({"PKG_X402": "0", "PKG_X402_PAY_TO": None}, PKG_BASE, "x402 is off"),
        ({"PKG_X402_PAY_TO": FINDER_BASE, "FINDER_X402_PAY_TO": FINDER_BASE}, FINDER_BASE,
         "would not mount"),  # another service's Base address
    ],
)  # fmt: skip
def test_a_wrong_package_recipient_stops_the_deploy(
    preflight, tmp_path, change, expect, needle, finder_env
):
    env = _env(tmp_path, **PKG)
    for k, v in change.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    problems = _pkg_run(preflight, env, expect)
    assert problems and needle in problems[0], problems


def test_package_check_off_leaves_the_deploy_unchanged(preflight, tmp_path, finder_env):
    assert _pkg_run(preflight, _env(tmp_path), None) == []
    stray = _pkg_run(preflight, _env(tmp_path), PKG_BASE)
    assert stray and "isn't configured" in stray[0]
