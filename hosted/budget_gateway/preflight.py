"""Pre-deploy check for Inferrail Hosted's payment configuration. Run it before the new process
starts, e.g. as the platform's pre-deploy command:

    python3 hosted/budget_gateway/preflight.py --expect-pay-to 0xYourReceivingAddress

It exits non-zero, and so stops the deploy, when the environment would either crash the service
at startup or collect payments differently from what was approved:
- the application can't be built with these settings: it builds the same combined app the
  service starts (every import and route, including ones that only exist when payments are
  on), on a throwaway data directory;
- Job-Safe Search is configured (`SEARCH_PAY_TO` set) and the shared-host app it starts
  (`hosted/job_safe_search/cohost.py`) can't be built: Search's own startup checks (approval
  flags, supplier, exclusions file, recovery block, distinct merchant) run here, on a throwaway
  database, instead of at startup inside the shared process;
- the settings themselves don't load (e.g. mainnet without BG_X402_MAINNET_APPROVED=1 or
  BG_BASE_RPC_URL): the same check the service runs at startup, run before it can take the
  shared process down;
- a receiving address is set but isn't exactly the expected one (or payments are on and no
  expected address was given);
- mainnet without the facilitator API key, or with a wallet secret present (this service must
  never be able to sign for a wallet);
- the RPC isn't the configured chain, or the facilitator doesn't support `exact` on it.

With payments off (no BG_X402_PAY_TO) it only checks that the settings load. It prints names
and public values only, never secret values.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

CHAIN_IDS = {"eip155:8453": "0x2105", "eip155:84532": "0x14a34"}


def rpc_chain_id(url: str) -> str:
    import httpx

    r = httpx.post(
        url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}, timeout=20
    )
    return str(r.json().get("result"))


def facilitator_kinds(env: dict[str, str]) -> set[tuple[str, str]]:
    """(scheme, network) pairs the configured facilitator supports, fetched with the API key
    (and never the wallet secret)."""
    from cdp.x402 import create_facilitator_config
    from x402.http import HTTPFacilitatorClient

    client = HTTPFacilitatorClient(
        create_facilitator_config(
            api_key_id=env["CDP_API_KEY_ID"], api_key_secret=env["CDP_API_KEY_SECRET"]
        )
    )
    result: Any = client.get_supported()
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    data = result.model_dump() if hasattr(result, "model_dump") else result
    return {(str(k.get("scheme")), str(k.get("network"))) for k in data.get("kinds", [])}


def build_app(settings: Any) -> None:
    """Build the combined app exactly as `cohost.py` does at startup, on a throwaway data
    directory (the real disk isn't mounted during a pre-deploy step), then release it."""
    import cohost

    with tempfile.TemporaryDirectory() as tmp:
        hosted = dataclasses.replace(settings, data_dir=Path(tmp) / "hosted")
        app = cohost.create_combined_app(Path(tmp) / "trial", hosted)
        app.state.hosted_app.state.release_instance_lock()


SEARCH_DB_KEYS = ("SEARCH_DB_PATH", "SEARCH_ALLOW_NEW_DB")


def build_search_app(settings: Any, env: dict[str, str]) -> None:
    """Build the shared-host app exactly as `hosted/job_safe_search/cohost.py` does, so Search's
    own configuration checks run before the deploy. The pre-deploy step has no disk, so only the
    database path is swapped for a throwaway one (not under /tmp, which Search refuses)."""
    repo = Path(__file__).resolve().parents[2]
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    saved = dict(os.environ)
    with tempfile.TemporaryDirectory(dir=repo, prefix=".preflight-") as tmp:
        try:
            os.environ.clear()
            os.environ.update(env)
            # Each app keeps its own, non-nested directory (Search refuses nested ones).
            (Path(tmp) / "search").mkdir()
            os.environ["SEARCH_DB_PATH"] = str(Path(tmp) / "search" / "search.sqlite3")
            os.environ["SEARCH_ALLOW_NEW_DB"] = "1"
            from hosted.job_safe_search import cohost as search_cohost
            from hosted.job_safe_search import service as search_service

            hosted = dataclasses.replace(settings, data_dir=Path(tmp) / "hosted")
            app = search_cohost.create_cohosted_app(
                Path(tmp) / "trial", hosted, search_service.production_app()
            )
            app.state.combined_app.state.hosted_app.state.release_instance_lock()
        finally:
            os.environ.clear()
            os.environ.update(saved)


def check(
    env: dict[str, str],
    expect_pay_to: str | None,
    *,
    chain_id: Callable[[str], str] = rpc_chain_id,
    kinds: Callable[[dict[str, str]], set[tuple[str, str]]] = facilitator_kinds,
    build: Callable[[Any], None] = build_app,
    build_search: Callable[[Any, dict[str, str]], None] = build_search_app,
    expect_search_pay_to: str | None = None,
    expect_read_pay_to: str | None = None,
    expect_web_search_pay_to: str | None = None,
    expect_read_extra: dict[str, str] | None = None,
    expect_finder_x402_pay_to: str | None = None,
    expect_domain_x402_pay_to: str | None = None,
    expect_pkg_x402_pay_to: str | None = None,
) -> list[str]:
    """Problems that must stop the deploy (empty = OK). Prints what it verified."""
    import cohost

    problems: list[str] = []
    service = cohost.load_modules()[1]
    try:
        if env.get("BG_MOUNT_PREFIX", cohost.PREFIX) != cohost.PREFIX:
            raise RuntimeError(f"BG_MOUNT_PREFIX must be {cohost.PREFIX} when cohosted")
        settings = service.Settings.from_env({**env, "BG_MOUNT_PREFIX": cohost.PREFIX})
    except Exception as ex:  # exactly what would stop the service at startup
        return [f"settings don't load, so the service would not start: {ex}"]
    try:
        build(settings)
        print("startup: the application builds with these settings (imports and routes)")
    except Exception as ex:  # e.g. an optional dependency missing from the production install
        return [f"the application would not start: {type(ex).__name__}: {str(ex)[:200]}"]
    if env.get("SEARCH_PAY_TO"):
        try:
            build_search(settings, env)
            print(
                f"search: builds; network {env.get('SEARCH_NETWORK', 'eip155:84532')}, "
                f"payTo {env['SEARCH_PAY_TO']}, price ${env.get('SEARCH_PRICE_USD', '0.01')}, "
                f"supplier {env.get('SEARCH_SUPPLIER', 'fixture')}"
            )
        except Exception as ex:
            return [f"Job-Safe Search would not start: {type(ex).__name__}: {str(ex)[:200]}"]
        if expect_search_pay_to and env["SEARCH_PAY_TO"].lower() != expect_search_pay_to.lower():
            problems.append(
                f"SEARCH_PAY_TO is {env['SEARCH_PAY_TO']}, not the approved {expect_search_pay_to}"
            )
    if env.get("READ_PAY_TO"):
        with tempfile.TemporaryDirectory() as tmp:  # never touch the real disk here
            web_read = cohost.web_read_from_env(settings, {**env, "READ_DATA_DIR": tmp})
        if web_read is None:
            problems.append(
                "Web Read is configured (READ_PAY_TO) but would not mount; the configuration "
                "error is logged above"
            )
        else:
            w = web_read[1]
            print(f"web read: builds; network {w.network}, payTo {w.pay_to}, price ${w.price_usd}")
            if expect_read_pay_to and w.pay_to.lower() != expect_read_pay_to.lower():
                problems.append(f"READ_PAY_TO is {w.pay_to}, not the approved {expect_read_pay_to}")
            problems += _check_read_extras(w, env, kinds, expect_read_extra or {})
            problems += _check_read_batch(cohost, settings, env, w)
    if env.get("WEB_SEARCH_PAY_TO"):
        with tempfile.TemporaryDirectory() as tmp:  # never touch the real disk here
            web_search = cohost.web_search_from_env(settings, {**env, "WEB_SEARCH_DATA_DIR": tmp})
        if web_search is None:
            problems.append(
                "Web Search is configured (WEB_SEARCH_PAY_TO) but would not mount; the "
                "configuration error is logged above"
            )
        else:
            s = web_search[1]
            print(
                f"web search: builds; payTo {s.pay_to}, price ${s.price_usd}, rails "
                f"{cohost._search_rails(s)}"
            )
            # A new receiving route: its address must be stated and checked, not optional.
            if not expect_web_search_pay_to:
                problems.append("Web Search is on but no --expect-web-search-pay-to was given")
            elif s.pay_to.lower() != expect_web_search_pay_to.lower():
                problems.append(
                    f"WEB_SEARCH_PAY_TO is {s.pay_to}, not the approved {expect_web_search_pay_to}"
                )
    if env.get("FINDER_PAY_TO"):
        with tempfile.TemporaryDirectory() as tmp:  # never touch the real disk here
            found = cohost.finder_from_env(settings, {**env, "FINDER_DATA_DIR": tmp})
        if found is None:
            problems.append(
                "x402 Finder is configured (FINDER_PAY_TO) but would not mount; the "
                "configuration error is logged above"
            )
        else:
            f = found[1]
            rails = cohost._search_rails(f)
            print(
                f"x402 finder: builds; price ${f.price_usd}, rails {rails}; "
                f"MPP payTo {f.pay_to}" + (f", x402 payTo {f.x402_recipient}" if f.x402 else "")
            )
            if f.mpp and expect_read_pay_to and f.pay_to.lower() != expect_read_pay_to.lower():
                problems.append(f"FINDER_PAY_TO (MPP) is {f.pay_to}, not Web Read's approved "
                                f"{expect_read_pay_to}")  # fmt: skip
            # A new x402 receiving route: its address must be stated and checked.
            if f.x402 and not expect_finder_x402_pay_to:
                problems.append("Finder x402 is on but no --expect-finder-x402-pay-to was given")
            elif f.x402 and f.x402_recipient.lower() != expect_finder_x402_pay_to.lower():
                problems.append(f"FINDER_X402_PAY_TO is {f.x402_recipient}, not the approved "
                                f"{expect_finder_x402_pay_to}")  # fmt: skip
    problems += _check_domain(cohost, settings, env, expect_read_pay_to, expect_domain_x402_pay_to)
    problems += _check_package(cohost, settings, env, expect_read_pay_to, expect_pkg_x402_pay_to)
    problems += check_offers(cohost, settings, env)
    if env.get("CDP_WALLET_SECRET"):
        problems.append(
            "CDP_WALLET_SECRET is set: remove it; this service never signs for a wallet"
        )
    pay_to = settings.x402_pay_to
    if not pay_to:
        print("payments: OFF (no BG_X402_PAY_TO)")
        return problems
    network = settings.x402_network
    print(f"payments: ON, network {network}, payTo {pay_to}, pack ${settings.x402_pack_price_usd}")
    if not expect_pay_to:
        problems.append("payments are on but no --expect-pay-to was given to check them against")
    elif pay_to != expect_pay_to:
        problems.append(f"BG_X402_PAY_TO is {pay_to}, not the approved {expect_pay_to}")
    if network == service.MAINNET:
        missing = [k for k in ("CDP_API_KEY_ID", "CDP_API_KEY_SECRET") if not env.get(k)]
        if missing:
            problems.append(f"mainnet needs {', '.join(missing)} (names only are shown)")
            return problems
    if settings.rpc_url:
        try:
            got = chain_id(settings.rpc_url)
            if got != CHAIN_IDS[network]:
                problems.append(f"BG_BASE_RPC_URL is chain {got}, not {network}")
            else:
                print(f"rpc: chain id {got} matches {network}")
        except Exception as ex:
            problems.append(f"BG_BASE_RPC_URL unreachable: {type(ex).__name__}")
    if network == service.MAINNET:
        try:
            if ("exact", network) in kinds(env):
                print(f"facilitator: authenticated; supports exact on {network}")
            else:
                problems.append(f"the facilitator does not support exact on {network}")
        except Exception as ex:
            problems.append(f"facilitator check failed with the API key: {type(ex).__name__}")
    return problems


def _offline_facilitator(s: Any) -> Any:
    """While the offer check asks an app for its 402, the x402 middleware's start-up lookup of
    what the facilitator supports is answered locally (exact on the route's own networks)
    instead of over the network. Whether the facilitator really supports them is checked
    separately, with the API key, by `check`."""
    from unittest import mock

    from x402.http import HTTPFacilitatorClient
    from x402.schemas import SupportedKind, SupportedResponse

    networks = [s.network, *dict(getattr(s, "extra_networks", ())).keys()]
    answer = SupportedResponse(
        kinds=[SupportedKind(x402_version=2, scheme="exact", network=n) for n in networks],
        extensions=[], signers={},
    )  # fmt: skip
    return mock.patch.object(HTTPFacilitatorClient, "get_supported", lambda self: answer)


def check_offers(cohost: Any, settings: Any, env: dict[str, str]) -> list[str]:
    """Each paid route's own unpaid 402, as this deploy would serve it, checked against the CDP
    facilitator's published verify schema (hosted/web_read/x402_contract.py). A buyer echoes the
    offer's resource and extensions into its payment, so a field out of that schema (on
    2026-11-23, a 501-character description where 500 is allowed) gets every payment refused at
    verify while the offer looks valid. No network call and no payment: the app is asked for its
    402 in-process, on a throwaway data directory."""
    from fastapi.testclient import TestClient

    contract = cohost._load("web_read_x402_contract",
                            cohost.HOSTED_DIR.parent / "web_read" / "x402_contract.py")  # fmt: skip
    routes = [
        ("web read", "READ_PAY_TO", cohost.web_read_from_env, ("READ_DATA_DIR",)),
        ("web read batch", "READ_BATCH", cohost.web_read_batch_from_env, ("READ_BATCH_DATA_DIR",)),
        ("x402 finder", "FINDER_PAY_TO", cohost.finder_from_env, ("FINDER_DATA_DIR",)),
        ("package check", "PKG_PAY_TO", cohost.package_check_from_env, ("PKG_DATA_DIR",)),
        ("domain check", "DOMAIN_PAY_TO", cohost.domain_check_from_env, ("DOMAIN_DATA_DIR",)),
    ]
    problems: list[str] = []
    for name, flag, factory, dirs in routes:
        if not env.get(flag):
            continue
        with tempfile.TemporaryDirectory() as tmp:  # never touch the real disk here
            mounted = factory(settings, {**env, "READ_DATA_DIR": tmp + "/r",
                                         **{d: f"{tmp}/{d.lower()}" for d in dirs}})  # fmt: skip
            if mounted is None:
                continue  # already reported by that route's own check
            app, s = mounted
            if not getattr(s, "x402", True):
                print(f"{name}: x402 off; no facilitator contract to check")
                continue
            task = app.state.task
            with _offline_facilitator(s):
                r = TestClient(app).post(f"{s.mount_prefix}{task.path}", json=task.input_example)
            header = r.headers.get("payment-required")
            if r.status_code != 402 or not header:
                problems.append(f"{name}: the unpaid request got HTTP {r.status_code}, not a 402 "
                                "with an x402 offer")  # fmt: skip
                continue
            try:
                found = contract.offer_problems(contract.decode_payment_required(header))
            except ImportError:
                problems.append(f"{name}: cdp-sdk is missing, so the offer can't be checked")
                continue
            if found:
                problems += [f"{name}: the facilitator would refuse every payment: {p}"
                             for p in found]  # fmt: skip
            else:
                print(f"{name}: offer passes the facilitator's verify schema")
    return problems


def _check_read_batch(cohost: Any, settings: Any, env: dict[str, str], read: Any) -> list[str]:
    """Web Read's batch (READ_BATCH=1) must mount and pay exactly Web Read's recipients: it is
    fail-isolated at startup, so without this check a misconfiguration would only leave it
    silently unmounted."""
    if env.get("READ_BATCH") != "1":
        return []
    with tempfile.TemporaryDirectory() as tmp:  # never touch the real disk here
        mounted = cohost.web_read_batch_from_env(
            settings, {**env, "READ_DATA_DIR": tmp, "READ_BATCH_DATA_DIR": tmp + "/batch"}
        )
    if mounted is None:
        return ["Web Read batch is on (READ_BATCH=1) but would not mount; the configuration "
                "error is logged above"]  # fmt: skip
    b = mounted[1]
    print(f"web read batch: builds; price ${b.price_usd} per page, payTo {b.pay_to}")
    if (b.pay_to.lower(), b.x402_recipient.lower(), b.price_usd) != (
        read.pay_to.lower(), read.x402_recipient.lower(), read.price_usd
    ):
        return ["Web Read batch would not pay Web Read's recipients at its price"]
    return []


def _check_domain(
    cohost: Any,
    settings: Any,
    env: dict[str, str],
    expect_read_pay_to: str | None,
    expect_x402: str | None,
) -> list[str]:
    """Domain Check's recipients, checked only when it is configured (DOMAIN_PAY_TO), so the
    other services' deploy is unchanged. MPP shares Web Read's Tempo address; Base x402 is a new
    receiving route, so its address must be stated with --expect-domain-x402-pay-to and match."""
    if not env.get("DOMAIN_PAY_TO"):
        if expect_x402:
            return ["--expect-domain-x402-pay-to was given but Domain Check isn't configured "
                    "(no DOMAIN_PAY_TO)"]  # fmt: skip
        return []
    with tempfile.TemporaryDirectory() as tmp:  # never touch the real disk here
        mounted = cohost.domain_check_from_env(settings, {**env, "DOMAIN_DATA_DIR": tmp})
    if mounted is None:
        return ["Domain Check is configured (DOMAIN_PAY_TO) but would not mount; the "
                "configuration error is logged above"]  # fmt: skip
    d = mounted[1]
    print(
        f"domain check: builds; price ${d.price_usd}, rails {cohost._search_rails(d)}; "
        f"MPP payTo {d.pay_to}" + (f", x402 payTo {d.x402_recipient}" if d.x402 else "")
    )
    problems: list[str] = []
    if d.mpp and expect_read_pay_to and d.pay_to.lower() != expect_read_pay_to.lower():
        problems.append(f"DOMAIN_PAY_TO (MPP) is {d.pay_to}, not Web Read's approved "
                        f"{expect_read_pay_to}")  # fmt: skip
    if d.x402 and not expect_x402:
        problems.append("Domain Check x402 is on but no --expect-domain-x402-pay-to was given")
    elif d.x402 and expect_x402 and d.x402_recipient.lower() != expect_x402.lower():
        problems.append(f"DOMAIN_X402_PAY_TO is {d.x402_recipient}, not the approved "
                        f"{expect_x402}")  # fmt: skip
    elif not d.x402 and expect_x402:
        problems.append("--expect-domain-x402-pay-to was given but Domain Check's x402 is off "
                        "(DOMAIN_X402=1 not set)")  # fmt: skip
    return problems


def _check_package(
    cohost: Any,
    settings: Any,
    env: dict[str, str],
    expect_read_pay_to: str | None,
    expect_x402: str | None,
) -> list[str]:
    """Package Check's recipients, checked only when it is configured (PKG_PAY_TO). Its Base
    address is checked when --expect-pkg-x402-pay-to is given; it is not yet required, so a deploy
    command written before this check existed keeps passing."""
    if not env.get("PKG_PAY_TO"):
        if expect_x402:
            return ["--expect-pkg-x402-pay-to was given but Package Check isn't configured "
                    "(no PKG_PAY_TO)"]  # fmt: skip
        return []
    with tempfile.TemporaryDirectory() as tmp:  # never touch the real disk here
        mounted = cohost.package_check_from_env(settings, {**env, "PKG_DATA_DIR": tmp})
    if mounted is None:
        return ["Package Check is configured (PKG_PAY_TO) but would not mount; the "
                "configuration error is logged above"]  # fmt: skip
    k = mounted[1]
    print(
        f"package check: builds; price ${k.price_usd}, rails {cohost._search_rails(k)}; "
        f"MPP payTo {k.pay_to}" + (f", x402 payTo {k.x402_recipient}" if k.x402 else "")
    )
    problems: list[str] = []
    if k.mpp and expect_read_pay_to and k.pay_to.lower() != expect_read_pay_to.lower():
        problems.append(f"PKG_PAY_TO (MPP) is {k.pay_to}, not Web Read's approved "
                        f"{expect_read_pay_to}")  # fmt: skip
    if k.x402 and expect_x402 and k.x402_recipient.lower() != expect_x402.lower():
        problems.append(f"PKG_X402_PAY_TO is {k.x402_recipient}, not the approved "
                        f"{expect_x402}")  # fmt: skip
    elif not k.x402 and expect_x402:
        problems.append("--expect-pkg-x402-pay-to was given but Package Check's x402 is off "
                        "(PKG_X402=1 not set)")  # fmt: skip
    return problems


def _check_read_extras(
    w: Any,
    env: dict[str, str],
    kinds: Callable[[dict[str, str]], set[tuple[str, str]]],
    expected: dict[str, str],
) -> list[str]:
    """Each extra Web Read network is a new receiving route: its address must be stated with
    --expect-read-extra network=payTo and match, and the facilitator must support exact on it."""
    problems: list[str] = []
    extras = dict(getattr(w, "extra_networks", ()))
    for network, pay_to in extras.items():
        print(f"web read: also accepts {network}, payTo {pay_to}")
        want = expected.get(network)
        if want is None:
            problems.append(
                f"Web Read accepts {network} but no --expect-read-extra was given for it"
            )
        elif want != pay_to:
            problems.append(f"Web Read's {network} payTo is {pay_to}, not the approved {want}")
    for network in expected:
        if network not in extras:
            problems.append(f"--expect-read-extra names {network}, which Web Read doesn't accept")
    if extras:
        try:
            supported = kinds(env)
        except Exception as ex:  # noqa: BLE001
            return [*problems, f"facilitator check failed: {type(ex).__name__}"]
        for network in extras:
            if ("exact", network) not in supported:
                problems.append(f"the facilitator does not support exact on {network}")
    return problems


def _pairs(values: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for v in values or []:
        network, _, pay_to = v.partition("=")
        out[network.strip()] = pay_to.strip()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--expect-pay-to", help="the approved receiving address (EIP-55)")
    ap.add_argument("--expect-search-pay-to", help="Job-Safe Search's approved receiving address")
    ap.add_argument("--expect-read-pay-to", help="Web Read's approved receiving address")
    ap.add_argument(
        "--expect-web-search-pay-to", help="Web Search's approved receiving address (required)"
    )
    ap.add_argument(
        "--expect-finder-x402-pay-to", help="x402 Finder's approved Base receiving address"
    )
    ap.add_argument(
        "--expect-domain-x402-pay-to", help="Domain Check's approved Base receiving address"
    )
    ap.add_argument(
        "--expect-pkg-x402-pay-to", help="Package Check's approved Base receiving address"
    )
    ap.add_argument(
        "--expect-read-extra", action="append", metavar="NETWORK=PAYTO",
        help="an approved extra Web Read network and its receiving address (repeat per network)",
    )  # fmt: skip
    args = ap.parse_args(argv)
    problems = check(
        dict(os.environ),
        args.expect_pay_to,
        expect_search_pay_to=args.expect_search_pay_to,
        expect_read_pay_to=args.expect_read_pay_to,
        expect_web_search_pay_to=args.expect_web_search_pay_to,
        expect_read_extra=_pairs(args.expect_read_extra),
        expect_finder_x402_pay_to=args.expect_finder_x402_pay_to,
        expect_domain_x402_pay_to=args.expect_domain_x402_pay_to,
        expect_pkg_x402_pay_to=args.expect_pkg_x402_pay_to,
    )
    for p in problems:
        print(f"PREFLIGHT FAILED: {p}")
    print("PREFLIGHT OK" if not problems else "deploy stopped; the running version keeps serving")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
